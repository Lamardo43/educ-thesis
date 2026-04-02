"""
Асинхронный SSH-клиент — обёртка над asyncssh.

Инкапсулирует детали установки соединения, предоставляя
высокоуровневый интерфейс для Lifecycle Manager.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import asyncssh

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SSHCredentials:
    """Параметры подключения к целевому хосту."""

    hostname: str
    port: int
    username: str
    password: str


@dataclass(frozen=True)
class CommandResult:
    """Результат выполнения SSH-команды."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0


class AsyncSSHClient:
    """Асинхронный клиент SSH/SCP для взаимодействия с целевыми хостами."""

    def __init__(self, credentials: SSHCredentials, connect_timeout: float = 10.0) -> None:
        self._creds = credentials
        self._connect_timeout = connect_timeout

    async def run_command(self, command: str, timeout: float = 30.0) -> CommandResult:
        """
        Выполнить команду на удалённом хосте.

        Args:
            command: Строка команды для исполнения в shell.
            timeout: Таймаут выполнения команды в секундах.

        Returns:
            CommandResult с кодом завершения и потоками stdout/stderr.
        """
        try:
            async with asyncssh.connect(
                host=self._creds.hostname,
                port=self._creds.port,
                username=self._creds.username,
                password=self._creds.password,
                known_hosts=None,
                connect_timeout=self._connect_timeout,
            ) as conn:
                result = await asyncio.wait_for(
                    conn.run(command, check=False),
                    timeout=timeout,
                )
                return CommandResult(
                    exit_code=result.exit_status or 0,
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                )
        except asyncio.TimeoutError:
            logger.error("SSH command timed out: %s@%s: %s", self._creds.username, self._creds.hostname, command)
            return CommandResult(exit_code=-1, stdout="", stderr="Timeout")
        except (asyncssh.Error, OSError) as exc:
            logger.error("SSH error: %s@%s: %s", self._creds.username, self._creds.hostname, exc)
            return CommandResult(exit_code=-1, stdout="", stderr=str(exc))

    async def copy_to(self, local_path: str, remote_path: str) -> bool:
        """
        Скопировать файл на удалённый хост через SCP.

        Args:
            local_path: Путь к файлу на локальной машине.
            remote_path: Путь назначения на удалённом хосте.

        Returns:
            True при успешном копировании.
        """
        try:
            async with asyncssh.connect(
                host=self._creds.hostname,
                port=self._creds.port,
                username=self._creds.username,
                password=self._creds.password,
                known_hosts=None,
                connect_timeout=self._connect_timeout,
            ) as conn:
                await asyncssh.scp(
                    local_path,
                    (conn, remote_path),
                )
            logger.info(
                "SCP: %s -> %s:%s",
                local_path,
                self._creds.hostname,
                remote_path,
            )
            return True
        except (asyncssh.Error, OSError) as exc:
            logger.error("SCP error: %s", exc)
            return False

    async def check_connectivity(self) -> bool:
        """Проверить доступность SSH-соединения."""
        result = await self.run_command("echo ok", timeout=10.0)
        return result.success and "ok" in result.stdout
