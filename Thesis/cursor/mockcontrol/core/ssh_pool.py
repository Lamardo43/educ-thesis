"""Пул постоянных SSH-соединений (asyncssh), одно соединение на хост."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import asyncssh
from asyncssh import SSHClientConnection, SSHCompletedProcess

from mockcontrol.core.exceptions import SCPError, SSHConnectionError

logger = logging.getLogger(__name__)


@dataclass
class _PooledConnection:
    conn: SSHClientConnection
    username: str


class SSHPool:
    """
    Persistent SSH: одно соединение на ``(hostname, port)``, переиспользование
    для команд и SCP.

    Перед повторным использованием выполняется проверка ``true`` с таймаутом;
    при сбое соединение закрывается и создаётся заново.

    Локальные хосты сюда не передавать — вызывающий код обязан обходиться без SSH.
    """

    def __init__(self, *, health_check_timeout: float = 5.0) -> None:
        self._health_check_timeout = health_check_timeout
        self._meta_lock = asyncio.Lock()
        self._host_locks: Dict[str, asyncio.Lock] = {}
        self._entries: Dict[str, _PooledConnection] = {}

    @staticmethod
    def _host_key(hostname: str, port: int) -> str:
        return f"{hostname}:{port}"

    async def _ensure_host_lock(self, key: str) -> asyncio.Lock:
        async with self._meta_lock:
            if key not in self._host_locks:
                self._host_locks[key] = asyncio.Lock()
            return self._host_locks[key]

    async def _dispose_conn(self, conn: Optional[SSHClientConnection]) -> None:
        if conn is None:
            return
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), timeout=15.0)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.debug("wait_closed after dispose: %s", exc)

    async def _probe_connection(self, conn: SSHClientConnection) -> bool:
        try:
            await conn.run("true", check=True, timeout=self._health_check_timeout)
            return True
        except Exception as exc:
            logger.debug("SSH health check failed: %s", exc)
            return False

    async def _open_connection(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
    ) -> SSHClientConnection:
        try:
            return await asyncssh.connect(
                hostname,
                port=port,
                username=username,
                password=password,
                known_hosts=None,
                keepalive_interval=30,
                keepalive_count_max=3,
            )
        except asyncssh.Error as exc:
            raise SSHConnectionError(
                f"Не удалось установить SSH-соединение с {hostname}:{port}: {exc}"
            ) from exc

    async def get_connection(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
    ) -> SSHClientConnection:
        """
        Возвращает живое SSH-соединение для хоста, при необходимости создаёт
        или пересоздаёт его (под ``asyncio.Lock``, привязанным к хосту).
        """
        key = self._host_key(hostname, port)
        host_lock = await self._ensure_host_lock(key)

        async with host_lock:
            entry = self._entries.get(key)

            if entry is not None and entry.username != username:
                await self._dispose_conn(entry.conn)
                del self._entries[key]
                entry = None

            if entry is not None:
                if await self._probe_connection(entry.conn):
                    return entry.conn
                await self._dispose_conn(entry.conn)
                del self._entries[key]

            conn = await self._open_connection(hostname, port, username, password)
            self._entries[key] = _PooledConnection(conn=conn, username=username)
            return conn

    async def run_command(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
        command: str,
        *,
        check: bool = False,
        timeout: Optional[float] = None,
    ) -> SSHCompletedProcess:
        """Выполняет команду на удалённом хосте через пул."""
        conn = await self.get_connection(hostname, port, username, password)
        try:
            return await conn.run(command, check=check, timeout=timeout)
        except (asyncssh.Error, asyncio.TimeoutError, asyncssh.ProcessError) as exc:
            raise SSHConnectionError(
                f"Ошибка выполнения команды по SSH ({hostname}:{port}): {exc}"
            ) from exc

    async def scp_upload(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: Optional[float] = None,
    ) -> None:
        """Загружает локальный файл на удалённый хост по SCP (через существующее соединение)."""
        conn = await self.get_connection(hostname, port, username, password)
        src = str(Path(local_path))
        try:
            if timeout is not None:
                await asyncio.wait_for(
                    asyncssh.scp(src, (conn, remote_path)),
                    timeout=timeout,
                )
            else:
                await asyncssh.scp(src, (conn, remote_path))
        except asyncio.TimeoutError as exc:
            raise SCPError(f"Таймаут SCP при загрузке на {hostname}:{port}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise SCPError(f"Ошибка SCP на {hostname}:{port}: {exc}") from exc

    async def close_all(self) -> None:
        """Закрывает все соединения в пуле."""
        keys = list(self._entries.keys())
        for key in keys:
            lock = self._host_locks.get(key)
            if lock is None:
                continue
            async with lock:
                entry = self._entries.pop(key, None)
                if entry is not None:
                    await self._dispose_conn(entry.conn)
        logger.info("SSH pool: все соединения закрыты")
