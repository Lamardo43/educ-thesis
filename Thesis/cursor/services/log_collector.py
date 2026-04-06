"""Сбор stdout/stderr Java-процессов в Redis ``logs:{filename}`` (ТЗ, «Log Collector»).

Цикл раз в 2 секунды для заглушек в статусе RUNNING. Локально — aiofiles и позиция в памяти;
удалённо — ``wc -c`` и ``tail -c +N`` по SSH. Позиции и буферы незавершённых строк сбрасываются,
когда заглушка перестаёт быть RUNNING или удалена из реестра.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import Any

import aiofiles
from redis.asyncio import Redis

from config import Settings
from core.crypto import CryptoService
from core.host_resolver import is_local
from core.ssh_pool import SSHPool

logger = logging.getLogger(__name__)

SETTINGS_GLOBAL_KEY = "settings:global"
MOCKS_REGISTRY_KEY = "mocks:registry"

_COLLECT_INTERVAL_SEC = 2.0
_SSH_LOG_TIMEOUT_SEC = 30.0


def _mock_key(filename: str) -> str:
    return f"mocks:{filename}"


def _logs_key(filename: str) -> str:
    return f"logs:{filename}"


def _host_key(hostname: str) -> str:
    return f"hosts:{hostname}"


def _account_key(account_uuid: str) -> str:
    return f"accounts:{account_uuid}"


def _log_path_for_filename(filename: str) -> str:
    """Совпадает с ``services.lifecycle._log_path_for_filename``."""
    safe = filename.replace("/", "_").replace("\x00", "")
    return f"/tmp/mockcontrol_{safe}.log"


def _ssh_cmd_succeeded(proc: Any) -> bool:
    es = getattr(proc, "exit_status", None)
    if es is not None:
        return int(es) == 0
    rc = getattr(proc, "returncode", None)
    if rc is not None:
        return int(rc) == 0
    return False


def _split_into_lines(chunk: str, pending: str) -> tuple[list[str], str]:
    """Делит ``pending + chunk`` на полные строки (по ``\\n``); хвост без ``\\n`` возвращает в pending."""
    buf = pending + chunk
    lines: list[str] = []
    start = 0
    while True:
        nl = buf.find("\n", start)
        if nl < 0:
            return lines, buf[start:]
        line = buf[start:nl].rstrip("\r")
        lines.append(line)
        start = nl + 1


class LogCollector:
    """Фоновый цикл: чтение новых байт лог-файла и RPUSH + LTRIM в Redis."""

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
        # 1-based индекс первого байта для ``tail -c +N`` (и согласованный seek для локального чтения).
        self._byte_positions: dict[str, int] = {}
        self._line_pending: dict[str, str] = {}

    def forget_mock(self, filename: str) -> None:
        """Удаляет отслеживание позиции и буфера строк (например, при остановке из lifecycle)."""
        self._byte_positions.pop(filename, None)
        self._line_pending.pop(filename, None)

    async def _retention_lines(self) -> int:
        raw = await self._redis.hget(SETTINGS_GLOBAL_KEY, "log_retention_lines")
        if raw is None or raw == "":
            return int(self._settings.default_log_retention_lines)
        try:
            v = int(raw)
            return v if v >= 1 else int(self._settings.default_log_retention_lines)
        except ValueError:
            return int(self._settings.default_log_retention_lines)

    async def _load_account(self, account_uuid: str) -> dict[str, str] | None:
        data = await self._redis.hgetall(_account_key(account_uuid))
        return data if data else None

    async def _ssh_creds_for_host(self, host: dict[str, str], hostname: str) -> tuple[str, str, int] | None:
        account_uuid = (host.get("account_uuid") or "").strip()
        if not account_uuid:
            logger.warning("LogCollector: host %r has no account_uuid", hostname)
            return None
        acc = await self._load_account(account_uuid)
        if not acc:
            return None
        enc = acc.get("password_enc", "")
        username = acc.get("username", "")
        if not username or not enc:
            return None
        try:
            password = self._crypto.decrypt(enc)
        except ValueError:
            logger.exception("LogCollector: failed to decrypt password for %r", hostname)
            return None
        try:
            port = int(host.get("ssh_port", "22"))
        except ValueError:
            return None
        return username, password, port

    def _prune_state_for_non_running(self, running: set[str]) -> None:
        for fn in list(self._byte_positions):
            if fn not in running:
                del self._byte_positions[fn]
        for fn in list(self._line_pending):
            if fn not in running:
                del self._line_pending[fn]

    async def _remote_file_size(
        self,
        hostname: str,
        username: str,
        password: str,
        ssh_port: int,
        path: str,
    ) -> int | None:
        q = shlex.quote(path)
        proc = await self._ssh.run_command(
            hostname,
            username,
            password,
            f"wc -c {q}",
            port=ssh_port,
            timeout=_SSH_LOG_TIMEOUT_SEC,
        )
        if not _ssh_cmd_succeeded(proc):
            return None
        out = (proc.stdout or "").strip().split()
        if not out:
            return None
        try:
            return int(out[0])
        except ValueError:
            return None

    async def _remote_read_from(
        self,
        hostname: str,
        username: str,
        password: str,
        ssh_port: int,
        path: str,
        last_position: int,
    ) -> bytes:
        q = shlex.quote(path)
        proc = await self._ssh.run_command(
            hostname,
            username,
            password,
            f"tail -c +{int(last_position)} {q}",
            port=ssh_port,
            timeout=_SSH_LOG_TIMEOUT_SEC,
        )
        if not _ssh_cmd_succeeded(proc):
            return b""
        raw = proc.stdout
        if raw is None:
            return b""
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return raw if isinstance(raw, bytes) else bytes(raw)

    async def _read_new_bytes_local(self, filename: str, path: str) -> None:
        pos = self._byte_positions.get(filename, 1)

        def stat_size() -> int | None:
            try:
                return os.path.getsize(path)
            except OSError:
                return None

        size = await asyncio.to_thread(stat_size)
        if size is None:
            return

        if pos > size + 1:
            pos = 1
            self._byte_positions[filename] = pos

        if pos > size:
            return

        async with aiofiles.open(path, "rb") as f:
            await f.seek(pos - 1)
            data = await f.read()
            tell = await f.tell()
        new_pos = tell + 1
        if data:
            await self._push_lines_from_chunk(filename, data.decode("utf-8", errors="replace"))
        self._byte_positions[filename] = new_pos

    async def _read_new_bytes_remote(
        self,
        filename: str,
        hostname: str,
        username: str,
        password: str,
        ssh_port: int,
        path: str,
    ) -> None:
        pos = self._byte_positions.get(filename, 1)
        size = await self._remote_file_size(hostname, username, password, ssh_port, path)
        if size is None:
            return

        if pos > size + 1:
            pos = 1
            self._byte_positions[filename] = pos

        if pos > size:
            return

        data = await self._remote_read_from(hostname, username, password, ssh_port, path, pos)
        new_pos = pos + len(data)
        if data:
            await self._push_lines_from_chunk(filename, data.decode("utf-8", errors="replace"))
        self._byte_positions[filename] = new_pos

    async def _push_lines_from_chunk(self, filename: str, text: str) -> None:
        if not text:
            return
        pending = self._line_pending.get(filename, "")
        lines, rest = _split_into_lines(text, pending)
        self._line_pending[filename] = rest
        if not lines:
            return
        retention = await self._retention_lines()
        key = _logs_key(filename)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, *lines)
            pipe.ltrim(key, -retention, -1)
            await pipe.execute()

    async def _collect_one_mock(self, filename: str, data: dict[str, str]) -> None:
        hostname = (data.get("hostname") or "").strip()
        if not hostname:
            return
        path = _log_path_for_filename(filename)

        try:
            if is_local(hostname):
                await self._read_new_bytes_local(filename, path)
                return

            host = await self._redis.hgetall(_host_key(hostname))
            if not host:
                logger.warning("LogCollector: host %r not found for %r", hostname, filename)
                return
            creds = await self._ssh_creds_for_host(host, hostname)
            if creds is None:
                return
            u, p, sp = creds
            await self._read_new_bytes_remote(filename, hostname, u, p, sp, path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LogCollector: log collection error for %r", filename)

    async def _iteration(self) -> None:
        filenames = await self._redis.smembers(MOCKS_REGISTRY_KEY)
        running: set[str] = set()
        running_data: dict[str, dict[str, str]] = {}

        for fn in filenames:
            try:
                h = await self._redis.hgetall(_mock_key(fn))
                if not h:
                    continue
                if (h.get("status") or "").strip() == "RUNNING":
                    running.add(fn)
                    running_data[fn] = h
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LogCollector: failed to read mock %r from Redis", fn)

        self._prune_state_for_non_running(running)

        for fn in sorted(running):
            await self._collect_one_mock(fn, running_data[fn])

    async def run(self) -> None:
        """Бесконечный цикл до отмены задачи."""
        while True:
            try:
                await self._iteration()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LogCollector: iteration failed")
            try:
                await asyncio.sleep(_COLLECT_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise


async def run_log_collector(
    redis: Redis,
    ssh_pool: SSHPool,
    crypto: CryptoService,
    settings: Settings,
    *,
    collector: LogCollector | None = None,
) -> None:
    """Корутина для ``asyncio.create_task``; при shutdown — ``task.cancel()``.

    Если передать тот же экземпляр ``LogCollector`` в lifespan (например ``app.state``),
    можно вызывать ``forget_mock`` при остановке заглушки до следующей итерации цикла.
    """
    c = collector or LogCollector(redis, ssh_pool, crypto, settings)
    await c.run()
