"""Фоновая проверка доступности хостов и работоспособности запущенных заглушек."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import httpx

from mockcontrol.core.crypto import CryptoService
from mockcontrol.core.exceptions import DecryptionError, SSHConnectionError
from mockcontrol.models.host import HostStatus

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from mockcontrol.core.ssh_pool import SSHPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthCheckerConfig:
    """Интервал между полными итерациями проверки (секунды)."""

    host_check_interval_sec: int

    def __post_init__(self) -> None:
        if self.host_check_interval_sec < 1:
            raise ValueError("host_check_interval_sec must be >= 1")


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _looks_like_filesystem_path(java_path: str) -> bool:
    jp = java_path.strip()
    if not jp:
        return False
    if jp.startswith(("/", ".", "~")):
        return True
    if os.name == "nt" and len(jp) > 1 and jp[1] == ":":
        return True
    return os.sep in jp or "/" in jp


def _pid_alive_local(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


async def _pid_alive_remote(
    ssh_pool: SSHPool,
    hostname: str,
    ssh_port: int,
    username: str,
    password: str,
    pid: int,
) -> bool:
    try:
        proc = await ssh_pool.run_command(
            hostname,
            ssh_port,
            username,
            password,
            f"kill -0 {int(pid)} 2>/dev/null && echo ok || echo no",
            check=False,
            timeout=15.0,
        )
    except SSHConnectionError:
        return False
    except Exception as exc:
        logger.warning("SSH kill -0 для PID %s на %s: %s", pid, hostname, exc)
        return False
    return (proc.stdout or "").strip() == "ok"


async def _http_mock_healthy(client: httpx.AsyncClient, base: str) -> bool:
    try:
        r = await client.get(f"{base}/")
    except httpx.TimeoutException:
        return False
    except httpx.RequestError:
        return False
    return r.status_code < 500


async def _check_single_host(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    host_resolver: ModuleType,
    hostname: str,
    host_row: dict[str, str],
) -> None:
    now = _utc_iso()
    java_path = host_row.get("java_path") or ""

    try:
        if host_resolver.is_local(hostname):
            # По ТЗ локальный хост всегда AVAILABLE; опционально — предупреждение в лог.
            jp = (java_path or "").strip()
            if jp and _looks_like_filesystem_path(jp) and not Path(jp).expanduser().is_file():
                logger.warning(
                    "Локальный хост %s: java_path не указывает на файл: %s",
                    hostname,
                    jp,
                )
            status = HostStatus.AVAILABLE
        else:
            ssh_port = int(host_row.get("ssh_port") or "22")
            account_uuid = host_row.get("account_uuid") or ""
            acc = await redis.hgetall(f"accounts:{account_uuid}")
            acc = dict(acc) if acc else {}
            if not acc:
                logger.error("Хост %s: учётная запись %s не найдена", hostname, account_uuid)
                status = HostStatus.UNAVAILABLE
            else:
                try:
                    password = crypto.decrypt(acc.get("password_enc") or "")
                except DecryptionError as exc:
                    logger.error("Хост %s: ошибка расшифровки пароля: %s", hostname, exc)
                    status = HostStatus.UNAVAILABLE
                else:
                    username = acc.get("username") or ""
                    try:
                        await ssh_pool.run_command(
                            hostname,
                            ssh_port,
                            username,
                            password,
                            "echo ok",
                            check=True,
                            timeout=10.0,
                        )
                        status = HostStatus.AVAILABLE
                    except SSHConnectionError as exc:
                        logger.warning("Хост %s: SSH недоступен: %s", hostname, exc)
                        status = HostStatus.UNAVAILABLE
                    except Exception as exc:
                        logger.warning("Хост %s: ошибка SSH-проверки: %s", hostname, exc)
                        status = HostStatus.UNAVAILABLE

        await redis.hset(
            f"hosts:{hostname}",
            mapping={
                "status": status.value,
                "last_checked_at": now,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Сбой проверки хоста %s", hostname)


async def _check_hosts(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    host_resolver: ModuleType,
) -> None:
    names = sorted(await redis.smembers("hosts:registry"))
    if not names:
        return
    pipe = redis.pipeline(transaction=False)
    for hn in names:
        pipe.hgetall(f"hosts:{hn}")
    rows = await pipe.execute()
    for hn, row in zip(names, rows, strict=True):
        row = dict(row) if row else {}
        if not row:
            logger.warning("hosts:registry: пустой hash для %s", hn)
            continue
        await _check_single_host(redis, ssh_pool, crypto, host_resolver, hn, row)


async def _check_running_mocks(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    host_resolver: ModuleType,
) -> None:
    names = sorted(await redis.smembers("mocks:registry"))
    if not names:
        return
    pipe = redis.pipeline(transaction=False)
    for m in names:
        pipe.hgetall(f"mocks:{m}")
    rows = await pipe.execute()

    running: list[tuple[str, dict[str, str]]] = []
    for name, row in zip(names, rows, strict=True):
        row = dict(row) if row else {}
        if (row.get("status") or "").upper() == "RUNNING":
            running.append((name, row))

    if not running:
        return

    timeout = httpx.Timeout(3.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http_client:
        for filename, mock in running:
            try:
                hostname = mock.get("hostname") or ""
                pid_raw = mock.get("pid")
                port_raw = mock.get("port")
                if not hostname or not pid_raw or not port_raw:
                    logger.warning(
                        "Заглушка %s: неполные hostname/pid/port, статус → ERROR",
                        filename,
                    )
                    await redis.hset(f"mocks:{filename}", mapping={"status": "ERROR"})
                    continue
                try:
                    pid = int(pid_raw)
                    port = int(port_raw)
                except ValueError:
                    logger.warning("Заглушка %s: некорректный pid/port", filename)
                    await redis.hset(f"mocks:{filename}", mapping={"status": "ERROR"})
                    continue

                pid_ok: bool
                if host_resolver.is_local(hostname):
                    pid_ok = _pid_alive_local(pid)
                else:
                    host_row = dict(await redis.hgetall(f"hosts:{hostname}"))
                    if not host_row:
                        logger.error(
                            "Заглушка %s: хост %s не найден для SSH-проверки PID",
                            filename,
                            hostname,
                        )
                        pid_ok = False
                    else:
                        ssh_port = int(host_row.get("ssh_port") or "22")
                        account_uuid = host_row.get("account_uuid") or ""
                        acc = dict(await redis.hgetall(f"accounts:{account_uuid}"))
                        if not acc:
                            logger.error(
                                "Заглушка %s: нет учётной записи %s",
                                filename,
                                account_uuid,
                            )
                            pid_ok = False
                        else:
                            try:
                                password = crypto.decrypt(acc.get("password_enc") or "")
                            except DecryptionError as exc:
                                logger.error(
                                    "Заглушка %s: расшифровка пароля: %s",
                                    filename,
                                    exc,
                                )
                                pid_ok = False
                            else:
                                username = acc.get("username") or ""
                                pid_ok = await _pid_alive_remote(
                                    ssh_pool,
                                    hostname,
                                    ssh_port,
                                    username,
                                    password,
                                    pid,
                                )

                base = f"http://{hostname}:{port}"
                http_ok = False
                if pid_ok:
                    http_ok = await _http_mock_healthy(http_client, base)

                if not pid_ok or not http_ok:
                    await redis.hset(f"mocks:{filename}", mapping={"status": "ERROR"})
                    logger.warning(
                        "Заглушка %s: health fail (pid_ok=%s, http_ok=%s)",
                        filename,
                        pid_ok,
                        http_ok,
                    )
                else:
                    await redis.hset(f"mocks:{filename}", mapping={"status": "RUNNING"})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Сбой health check заглушки %s", filename)


async def health_checker_loop(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    host_resolver: ModuleType,
    config: HealthCheckerConfig,
) -> None:
    """
    Бесконечный цикл проверок. Остановка: ``task.cancel()`` (shutdown приложения).
    """
    while True:
        try:
            await _check_hosts(redis, ssh_pool, crypto, host_resolver)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Фаза проверки хостов завершилась с ошибкой")

        try:
            await _check_running_mocks(redis, ssh_pool, crypto, host_resolver)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Фаза health check заглушек завершилась с ошибкой")

        try:
            await asyncio.sleep(config.host_check_interval_sec)
        except asyncio.CancelledError:
            raise


def start_health_checker_task(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    host_resolver: ModuleType,
    config: HealthCheckerConfig,
) -> asyncio.Task[None]:
    """Запускает :func:`health_checker_loop` через ``asyncio.create_task``."""
    return asyncio.create_task(
        health_checker_loop(redis, ssh_pool, crypto, host_resolver, config),
        name="mockcontrol-health-checker",
    )
