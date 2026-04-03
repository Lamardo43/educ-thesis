"""Фоновый сбор строк логов заглушек в Redis (lists logs:{filename})."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import aiofiles

from mockcontrol.core.crypto import CryptoService
from mockcontrol.core.exceptions import DecryptionError, SSHConnectionError

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from mockcontrol.core.ssh_pool import SSHPool

logger = logging.getLogger(__name__)


def _log_file_path(filename: str) -> str:
    safe = Path(filename).name
    return f"/tmp/mockcontrol_{safe}.log"


@dataclass(frozen=True)
class LogCollectorConfig:
    """Пауза между итерациями и лимит строк в Redis."""

    poll_interval_sec: int = 2
    log_retention_lines: int = 1000

    def __post_init__(self) -> None:
        if self.poll_interval_sec < 1:
            raise ValueError("poll_interval_sec must be >= 1")
        if self.log_retention_lines < 1:
            raise ValueError("log_retention_lines must be >= 1")


def _parse_wc_c(stdout: str) -> int | None:
    line = (stdout or "").strip().splitlines()
    if not line:
        return None
    first = line[0].strip().split()
    if not first:
        return None
    try:
        return int(first[0])
    except ValueError:
        return None


def _split_complete_lines(prefix: str, chunk: str) -> tuple[list[str], str]:
    text = prefix + chunk
    if not text:
        return [], ""
    parts = text.split("\n")
    if len(parts) == 1:
        return [], parts[0]
    complete = parts[:-1]
    return complete, parts[-1]


class LogCollector:
    """
    Следит за RUNNING-заглушками, дописывает новые строки в Redis.
    Позиции чтения (байты) и незавершённая строка — только в памяти процесса.
    """

    def __init__(
        self,
        redis: Redis,
        ssh_pool: SSHPool,
        crypto: CryptoService,
        host_resolver: ModuleType,
        config: LogCollectorConfig,
    ) -> None:
        self._redis = redis
        self._ssh_pool = ssh_pool
        self._crypto = crypto
        self._host_resolver = host_resolver
        self._config = config
        self._byte_positions: dict[str, int] = {}
        self._line_partial: dict[str, str] = {}

    def forget_mock(self, filename: str) -> None:
        """Сбросить состояние чтения (при остановке или удалении заглушки)."""
        self._byte_positions.pop(filename, None)
        self._line_partial.pop(filename, None)

    async def _list_running_mocks(self) -> list[tuple[str, dict[str, str]]]:
        names = sorted(await self._redis.smembers("mocks:registry"))
        if not names:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for m in names:
            pipe.hgetall(f"mocks:{m}")
        rows = await pipe.execute()
        out: list[tuple[str, dict[str, str]]] = []
        for name, row in zip(names, rows, strict=True):
            row = dict(row) if row else {}
            if (row.get("status") or "").upper() == "RUNNING":
                out.append((name, row))
        return out

    async def _ssh_credentials(
        self, hostname: str, host_row: dict[str, str]
    ) -> tuple[int, str, str] | None:
        ssh_port = int(host_row.get("ssh_port") or "22")
        account_uuid = host_row.get("account_uuid") or ""
        acc = dict(await self._redis.hgetall(f"accounts:{account_uuid}"))
        if not acc:
            logger.error("log_collector: нет учётной записи %s для %s", account_uuid, hostname)
            return None
        try:
            password = self._crypto.decrypt(acc.get("password_enc") or "")
        except DecryptionError as exc:
            logger.error("log_collector: расшифровка для %s: %s", hostname, exc)
            return None
        username = acc.get("username") or ""
        if not username:
            return None
        return ssh_port, username, password

    async def _collect_local(self, filename: str) -> None:
        path = _log_file_path(filename)
        try:
            size = await asyncio.to_thread(lambda: Path(path).stat().st_size)
        except OSError as exc:
            logger.debug("log_collector %s: нет файла или stat: %s", filename, exc)
            return

        pos = self._byte_positions.get(filename)
        if pos is None:
            self._byte_positions[filename] = size
            return

        if size < pos:
            pos = 0
            self._byte_positions[filename] = 0
            self._line_partial.pop(filename, None)

        read_from = self._byte_positions[filename]
        if size <= read_from:
            return

        try:
            async with aiofiles.open(path, "rb") as f:
                await f.seek(read_from)
                raw = await f.read()
        except OSError as exc:
            logger.warning("log_collector %s: чтение %s: %s", filename, path, exc)
            return

        if not raw:
            self._byte_positions[filename] = await asyncio.to_thread(
                lambda: Path(path).stat().st_size
            )
            return

        try:
            chunk = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("log_collector %s: decode: %s", filename, exc)
            return

        partial_in = self._line_partial.get(filename, "")
        lines, partial_out = _split_complete_lines(partial_in, chunk)
        self._line_partial[filename] = partial_out

        new_size = await asyncio.to_thread(lambda: Path(path).stat().st_size)
        self._byte_positions[filename] = new_size

        if lines:
            key = f"logs:{filename}"
            await self._redis.rpush(key, *lines)
            await self._redis.ltrim(key, -self._config.log_retention_lines, -1)

    async def _collect_remote(self, filename: str, hostname: str) -> None:
        path = _log_file_path(filename)
        quoted = shlex.quote(path)

        host_row = dict(await self._redis.hgetall(f"hosts:{hostname}"))
        if not host_row:
            logger.error("log_collector %s: хост %s не найден", filename, hostname)
            return

        creds = await self._ssh_credentials(hostname, host_row)
        if creds is None:
            return
        ssh_port, username, password = creds

        try:
            wc = await self._ssh_pool.run_command(
                hostname,
                ssh_port,
                username,
                password,
                f"wc -c < {quoted} 2>/dev/null || wc -c {quoted}",
                check=False,
                timeout=30.0,
            )
        except SSHConnectionError as exc:
            logger.warning("log_collector %s: wc -c SSH: %s", filename, exc)
            return
        except Exception as exc:
            logger.warning("log_collector %s: wc -c: %s", filename, exc)
            return

        size = _parse_wc_c(wc.stdout or "")
        if size is None:
            logger.debug("log_collector %s: не разобрать wc -c", filename)
            return

        pos = self._byte_positions.get(filename)
        if pos is None:
            self._byte_positions[filename] = size
            return

        if size < pos:
            pos = 0
            self._byte_positions[filename] = 0
            self._line_partial.pop(filename, None)

        read_from = self._byte_positions[filename]
        if size <= read_from:
            return

        # GNU tail: байты с 1-based индекса read_from+1 (read_from байт уже прочитаны).
        start = read_from + 1
        try:
            tail = await self._ssh_pool.run_command(
                hostname,
                ssh_port,
                username,
                password,
                f"tail -c +{start} {quoted} 2>/dev/null",
                check=False,
                timeout=60.0,
            )
        except SSHConnectionError as exc:
            logger.warning("log_collector %s: tail SSH: %s", filename, exc)
            return
        except Exception as exc:
            logger.warning("log_collector %s: tail: %s", filename, exc)
            return

        text = tail.stdout or ""
        if text:
            partial_in = self._line_partial.get(filename, "")
            lines, partial_out = _split_complete_lines(partial_in, text)
            self._line_partial[filename] = partial_out
            if lines:
                key = f"logs:{filename}"
                await self._redis.rpush(key, *lines)
                await self._redis.ltrim(key, -self._config.log_retention_lines, -1)

        try:
            wc2 = await self._ssh_pool.run_command(
                hostname,
                ssh_port,
                username,
                password,
                f"wc -c < {quoted} 2>/dev/null || wc -c {quoted}",
                check=False,
                timeout=30.0,
            )
        except Exception as exc:
            logger.debug("log_collector %s: wc после tail: %s", filename, exc)
            self._byte_positions[filename] = size
            return

        size2 = _parse_wc_c(wc2.stdout or "")
        self._byte_positions[filename] = size2 if size2 is not None else size

    async def _collect_one(self, filename: str, mock: dict[str, str]) -> None:
        hostname = (mock.get("hostname") or "").strip()
        if not hostname:
            return
        try:
            if self._host_resolver.is_local(hostname):
                await self._collect_local(filename)
            else:
                await self._collect_remote(filename, hostname)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("log_collector: сбой для %s", filename)

    async def run_forever(self) -> None:
        """Бесконечный цикл; остановка через ``task.cancel()``."""
        while True:
            try:
                running = await self._list_running_mocks()
                running_names = {fn for fn, _ in running}

                for fn, mock in running:
                    await self._collect_one(fn, mock)

                for fn in list(self._byte_positions.keys()):
                    if fn not in running_names:
                        self.forget_mock(fn)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("log_collector: ошибка итерации")

            try:
                await asyncio.sleep(self._config.poll_interval_sec)
            except asyncio.CancelledError:
                raise


def create_log_collector(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    host_resolver: ModuleType,
    config: LogCollectorConfig,
) -> LogCollector:
    """Собирает экземпляр; передайте его в :func:`start_log_collector_task` и храните ссылку для ``forget_mock``."""
    return LogCollector(redis, ssh_pool, crypto, host_resolver, config)


def start_log_collector_task(collector: LogCollector) -> asyncio.Task[None]:
    """Запускает :meth:`LogCollector.run_forever` в ``asyncio.create_task``."""
    return asyncio.create_task(collector.run_forever(), name="mockcontrol-log-collector")
