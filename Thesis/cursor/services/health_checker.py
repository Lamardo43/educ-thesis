"""Фоновая проверка доступности хостов и health check заглушек в статусе RUNNING (ТЗ, «Health Checker»).

Использует SSH-пул только для удалённых хостов. HTTP-проверка выполняется отдельным
``httpx.AsyncClient``, не из реестра прокси.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from redis.asyncio import Redis

from config import Settings
from core.crypto import CryptoService
from core.host_resolver import is_local
from core.ssh_pool import SSHPool

logger = logging.getLogger(__name__)

SETTINGS_GLOBAL_KEY = "settings:global"
HOSTS_REGISTRY_KEY = "hosts:registry"
MOCKS_REGISTRY_KEY = "mocks:registry"

_HOST_SSH_PROBE_TIMEOUT_SEC = 10.0
_MOCK_HTTP_TIMEOUT_SEC = 10.0
_HTTP_FAILS_TO_ERROR = 3


def _host_key(hostname: str) -> str:
    return f"hosts:{hostname}"


def _mock_key(filename: str) -> str:
    return f"mocks:{filename}"


def _account_key(account_uuid: str) -> str:
    return f"accounts:{account_uuid}"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ssh_cmd_succeeded(proc: Any) -> bool:
    es = getattr(proc, "exit_status", None)
    if es is not None:
        return int(es) == 0
    rc = getattr(proc, "returncode", None)
    if rc is not None:
        return int(rc) == 0
    return False


class HealthChecker:
    """Бесконечный цикл проверки хостов и RUNNING-заглушек с паузой ``host_check_interval_sec``."""

    def __init__(
        self,
        redis: Redis,
        ssh_pool: SSHPool,
        crypto: CryptoService,
        settings: Settings,
    ) -> None:
        self._redis = redis
        self._ssh = ssh_pool
        self._crypto = crypto
        self._settings = settings
        # Под нагрузкой кратковременный таймаут root-check не должен сразу переводить заглушку в ERROR.
        self._http_failures: dict[str, int] = {}

    async def _interval_sec(self) -> float:
        raw = await self._redis.hget(SETTINGS_GLOBAL_KEY, "host_check_interval_sec")
        if raw is None or raw == "":
            return float(self._settings.default_host_check_interval)
        try:
            v = int(raw)
            return float(v) if v >= 1 else float(self._settings.default_host_check_interval)
        except ValueError:
            return float(self._settings.default_host_check_interval)

    async def _load_account(self, account_uuid: str) -> dict[str, str] | None:
        data = await self._redis.hgetall(_account_key(account_uuid))
        return data if data else None

    async def _ssh_creds_for_host(self, host: dict[str, str], hostname: str) -> tuple[str, str, int] | None:
        account_uuid = (host.get("account_uuid") or "").strip()
        if not account_uuid:
            logger.warning("Host %r: account_uuid not set, SSH unavailable", hostname)
            return None
        acc = await self._load_account(account_uuid)
        if not acc:
            logger.warning("Host %r: account %r not found", hostname, account_uuid)
            return None
        enc = acc.get("password_enc", "")
        username = acc.get("username", "")
        if not username or not enc:
            logger.warning("Host %r: incomplete SSH account data", hostname)
            return None
        try:
            password = self._crypto.decrypt(enc)
        except ValueError:
            logger.exception("Host %r: failed to decrypt SSH password", hostname)
            return None
        try:
            port = int(host.get("ssh_port", "22"))
        except ValueError:
            logger.warning("Host %r: invalid ssh_port", hostname)
            return None
        return username, password, port

    async def _check_hosts(self) -> None:
        hostnames = await self._redis.smembers(HOSTS_REGISTRY_KEY)
        if not hostnames:
            return
        for hostname in sorted(hostnames):
            try:
                key = _host_key(hostname)
                data = await self._redis.hgetall(key)
                if not data:
                    logger.debug("Host %r is in registry but Redis key is empty — skip", hostname)
                    continue
                if is_local(hostname):
                    await self._redis.hset(
                        key,
                        mapping={"status": "AVAILABLE", "last_checked_at": _utc_iso()},
                    )
                    continue
                creds = await self._ssh_creds_for_host(data, hostname)
                if creds is None:
                    await self._redis.hset(
                        key,
                        mapping={"status": "UNAVAILABLE", "last_checked_at": _utc_iso()},
                    )
                    continue
                user, password, ssh_port = creds
                try:
                    proc = await self._ssh.run_command(
                        hostname,
                        user,
                        password,
                        "echo ok",
                        port=ssh_port,
                        timeout=_HOST_SSH_PROBE_TIMEOUT_SEC,
                    )
                    ok = _ssh_cmd_succeeded(proc)
                except Exception:
                    logger.exception("Host %r: SSH check failed (echo ok)", hostname)
                    ok = False
                status = "AVAILABLE" if ok else "UNAVAILABLE"
                await self._redis.hset(
                    key,
                    mapping={"status": status, "last_checked_at": _utc_iso()},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Host %r: unexpected error during check", hostname)

    async def _pid_alive_local(self, pid: int) -> bool:
        def check() -> bool:
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            return True

        return await asyncio.to_thread(check)

    async def _pid_alive_remote(
        self,
        hostname: str,
        username: str,
        password: str,
        ssh_port: int,
        pid: int,
    ) -> bool:
        try:
            proc = await self._ssh.run_command(
                hostname,
                username,
                password,
                f"kill -0 {int(pid)}",
                port=ssh_port,
                timeout=_HOST_SSH_PROBE_TIMEOUT_SEC,
            )
            return _ssh_cmd_succeeded(proc)
        except Exception:
            logger.exception(
                "Mock on %r: SSH kill -0 error for pid=%s",
                hostname,
                pid,
            )
            return False

    async def _http_root_ok(self, client: httpx.AsyncClient, hostname: str, port: int) -> bool:
        url = f"http://{hostname}:{port}/"
        try:
            response = await client.get(url)
            # Любой полученный ответ считается «сервис отвечает»; таймауты/обрыв — ошибка.
            _ = response.status_code
            return True
        except httpx.RequestError:
            return False
        except Exception:
            logger.exception("HTTP GET %s: unexpected error", url)
            return False

    async def _check_running_mock(
        self,
        filename: str,
        data: dict[str, str],
        http_client: httpx.AsyncClient,
    ) -> None:
        hostname = (data.get("hostname") or "").strip()
        if not hostname:
            logger.warning("Mock %r: empty hostname", filename)
            await self._redis.hset(_mock_key(filename), mapping={"status": "ERROR"})
            return
        try:
            port = int((data.get("port") or "").strip())
            pid = int((data.get("pid") or "").strip())
        except ValueError:
            logger.warning("Mock %r: invalid port/pid in Redis", filename)
            await self._redis.hset(_mock_key(filename), mapping={"status": "ERROR"})
            return

        pid_ok: bool
        if is_local(hostname):
            try:
                pid_ok = await self._pid_alive_local(pid)
            except Exception:
                logger.exception("Mock %r: local PID check error", filename)
                pid_ok = False
        else:
            host = await self._redis.hgetall(_host_key(hostname))
            if not host:
                logger.warning("Mock %r: host %r not found in Redis", filename, hostname)
                pid_ok = False
            else:
                creds = await self._ssh_creds_for_host(host, hostname)
                if creds is None:
                    pid_ok = False
                else:
                    u, p, sp = creds
                    pid_ok = await self._pid_alive_remote(hostname, u, p, sp, pid)

        http_ok = await self._http_root_ok(http_client, hostname, port)

        if not pid_ok or not http_ok:
            if not pid_ok:
                logger.warning(
                    "Mock %r: process not found (pid=%s on %r)",
                    filename,
                    pid,
                    hostname,
                )
                self._http_failures.pop(filename, None)
                await self._redis.hset(_mock_key(filename), mapping={"status": "ERROR"})
                return
            if not http_ok:
                attempts = self._http_failures.get(filename, 0) + 1
                self._http_failures[filename] = attempts
                logger.warning(
                    "Mock %r: HTTP GET to root failed (%r:%s), attempt %s/%s",
                    filename,
                    hostname,
                    port,
                    attempts,
                    _HTTP_FAILS_TO_ERROR,
                )
                if attempts >= _HTTP_FAILS_TO_ERROR:
                    await self._redis.hset(_mock_key(filename), mapping={"status": "ERROR"})
                return

        self._http_failures.pop(filename, None)
        await self._redis.hset(_mock_key(filename), mapping={"status": "RUNNING"})

    async def _check_mocks(self, http_client: httpx.AsyncClient) -> None:
        filenames = await self._redis.smembers(MOCKS_REGISTRY_KEY)
        if not filenames:
            return
        for filename in sorted(filenames):
            try:
                key = _mock_key(filename)
                data = await self._redis.hgetall(key)
                if not data:
                    continue
                status = (data.get("status") or "").strip()
                # ERROR может быть временным (например, кратковременный таймаут); продолжаем проверять,
                # чтобы заглушка могла автоматически вернуться в RUNNING без ручного вмешательства.
                if status not in {"RUNNING", "ERROR"}:
                    continue
                await self._check_running_mock(filename, data, http_client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Mock %r: health check error", filename)

    async def run(self) -> None:
        """Запускает бесконечный цикл до отмены задачи (``CancelledError``)."""
        timeout = httpx.Timeout(_MOCK_HTTP_TIMEOUT_SEC, connect=_MOCK_HTTP_TIMEOUT_SEC)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            while True:
                try:
                    interval = await self._interval_sec()
                    await self._check_hosts()
                    await self._check_mocks(http_client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("HealthChecker: iteration failed, loop continues")
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise


async def run_health_checker(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    settings: Settings,
) -> None:
    """Точка входа: фоновая задача проверки хостов и заглушек (см. lifespan приложения).

    При завершении приложения задачу следует отменить (`task.cancel()`); корутина
    закроет httpx-клиент и пробросит ``CancelledError``.
    """
    checker = HealthChecker(redis, ssh_pool, crypto, settings)
    await checker.run()
