"""Пул постоянных SSH-соединений (asyncssh): одно соединение на удалённый хост.

Пул не предназначен для локальных хостов — вызывающий код должен сначала проверить
`host_resolver.is_local()` и не обращаться к пулу для таких имён.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import asyncssh

from core.exceptions import SCPError, SSHConnectionError
from core.host_resolver import is_local

if TYPE_CHECKING:
    from asyncssh.connection import SSHClientConnection

logger = logging.getLogger(__name__)

_HEALTH_CHECK_CMD = "true"
_HEALTH_CHECK_TIMEOUT_SEC = 5.0
_CONNECT_KWARGS = {
    "known_hosts": None,
    "keepalive_interval": 30,
    "keepalive_count_max": 3,
}


@dataclass
class _HostEntry:
    """Состояние SSH для одного хоста: отдельный lock против гонок при создании conn."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conn: SSHClientConnection | None = None
    username: str | None = None
    password: str | None = None
    port: int | None = None


class SSHPool:
    """Один переиспользуемый SSH-клиент на хост; проверка живости перед использованием."""

    def __init__(self) -> None:
        self._registry_lock = asyncio.Lock()
        self._hosts: dict[str, _HostEntry] = {}

    async def _entry(self, hostname: str) -> _HostEntry:
        async with self._registry_lock:
            if hostname not in self._hosts:
                self._hosts[hostname] = _HostEntry()
            return self._hosts[hostname]

    @staticmethod
    def _reject_local(hostname: str) -> None:
        if is_local(hostname):
            raise ValueError(
                "SSHPool не используется для локальных хостов; проверьте host_resolver.is_local()"
            )

    async def _close_conn(self, entry: _HostEntry) -> None:
        if entry.conn is None:
            return
        conn = entry.conn
        entry.conn = None
        try:
            conn.close()
            await conn.wait_closed()
        except Exception:
            logger.debug("Error while closing SSH connection", exc_info=True)

    async def _probe_conn(self, conn: SSHClientConnection) -> bool:
        try:
            result = await asyncio.wait_for(
                conn.run(_HEALTH_CHECK_CMD, check=False),
                timeout=_HEALTH_CHECK_TIMEOUT_SEC,
            )
            if result.exit_status is not None:
                return result.exit_status == 0
            if result.returncode is not None:
                return result.returncode == 0
            return False
        except Exception:
            logger.debug("SSH health-check failed", exc_info=True)
            return False

    async def _open_conn(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
    ) -> SSHClientConnection:
        try:
            return await asyncio.wait_for(
                asyncssh.connect(
                    hostname,
                    port=port,
                    username=username,
                    password=password,
                    **_CONNECT_KWARGS,
                ),
                timeout=_HEALTH_CHECK_TIMEOUT_SEC * 2,
            )
        except asyncio.TimeoutError as exc:
            raise SSHConnectionError(
                f"Таймаут подключения SSH к {hostname!r}:{port}"
            ) from exc
        except Exception as exc:
            raise SSHConnectionError(
                f"Не удалось установить SSH-соединение с {hostname!r}:{port}"
            ) from exc

    async def get_connection(
        self,
        hostname: str,
        username: str,
        password: str,
        port: int = 22,
    ) -> SSHClientConnection:
        """Возвращает живое соединение к хосту (при необходимости создаёт или пересоздаёт)."""
        self._reject_local(hostname)
        entry = await self._entry(hostname)
        async with entry.lock:
            same_creds = (
                entry.username == username
                and entry.password == password
                and entry.port == port
            )
            if entry.conn is not None and same_creds:
                if await self._probe_conn(entry.conn):
                    return entry.conn
                await self._close_conn(entry)
            elif entry.conn is not None and not same_creds:
                await self._close_conn(entry)

            entry.conn = await self._open_conn(hostname, port, username, password)
            entry.username = username
            entry.password = password
            entry.port = port
            return entry.conn

    async def run_command(
        self,
        hostname: str,
        username: str,
        password: str,
        command: str,
        *,
        port: int = 22,
        timeout: float | None = None,
    ) -> asyncssh.SSHCompletedProcess:
        """Выполняет команду на удалённом хосте через пул."""
        self._reject_local(hostname)
        conn = await self.get_connection(hostname, username, password, port)
        try:
            return await conn.run(command, check=False, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise SSHConnectionError(
                f"Таймаут выполнения SSH-команды на {hostname!r}"
            ) from exc
        except Exception as exc:
            raise SSHConnectionError(
                f"Ошибка выполнения SSH-команды на {hostname!r}"
            ) from exc

    async def scp_upload(
        self,
        hostname: str,
        username: str,
        password: str,
        local_path: str | Path,
        remote_path: str,
        *,
        port: int = 22,
    ) -> None:
        """Загружает локальный файл на удалённый хост по SFTP поверх пулового соединения."""
        self._reject_local(hostname)
        conn = await self.get_connection(hostname, username, password, port)
        local = Path(local_path)
        if not local.is_file():
            raise SCPError(f"Локальный файл не найден: {local}")
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.put(str(local), remote_path)
        except SCPError:
            raise
        except Exception as exc:
            raise SCPError(
                f"Не удалось загрузить файл на {hostname!r}:{remote_path!r}"
            ) from exc

    async def close_all(self) -> None:
        """Закрывает все соединения и очищает реестр хостов."""
        async with self._registry_lock:
            entries = list(self._hosts.values())
            self._hosts.clear()
        for entry in entries:
            async with entry.lock:
                await self._close_conn(entry)
        logger.info("SSHPool: all connections closed")
