"""
Lifecycle Manager — управление жизненным циклом имитационных сервисов.

Отвечает за:
- Регистрацию заглушки (приём файла → SCP на хост → запись в Redis)
- Запуск Java-процесса на целевом хосте через SSH
- Остановку процесса (SIGTERM → SIGKILL)
- Удаление заглушки (остановка + удаление файла + удаление из Redis)
- Верификацию состояний при старте системы (reconciliation)
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from mockcontrol.core.crypto import CryptoService
from mockcontrol.models.host import HostConfig
from mockcontrol.models.mock import MockConfig, MockStatus
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService
from mockcontrol.ssh.client import AsyncSSHClient, SSHCredentials
from mockcontrol.ssh.operations import (
    check_process_alive,
    copy_artifact,
    delete_remote_file,
    find_free_port,
    read_log_tail,
    start_java_process,
    stop_process,
)

logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    """Ошибка операции жизненного цикла."""


class LifecycleManager:
    """
    Оркестрация жизненного цикла имитационных сервисов.

    Координирует взаимодействие между Redis (через сервисы),
    SSH-модулем и Crypto Service.
    """

    def __init__(
        self,
        mock_service: MockService,
        host_service: HostService,
        account_service: AccountService,
        crypto: CryptoService,
        tmp_dir: Path,
    ) -> None:
        self._mocks = mock_service
        self._hosts = host_service
        self._accounts = account_service
        self._crypto = crypto
        self._tmp_dir = tmp_dir

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    async def _build_ssh_client(self, host_config: HostConfig, hostname: str) -> AsyncSSHClient:
        """Создать SSH-клиент с расшифрованными credentials."""
        account = await self._accounts.get(host_config.account_uuid)
        if account is None:
            raise LifecycleError(
                f"Account {host_config.account_uuid} not found for host {hostname}"
            )

        password = self._crypto.decrypt(account.password_enc)
        credentials = SSHCredentials(
            hostname=hostname,
            port=host_config.ssh_port,
            username=account.username,
            password=password,
        )
        return AsyncSSHClient(credentials)

    def _artifact_remote_path(self, working_dir: str, filename: str) -> str:
        """Вычислить путь к артефакту на целевом хосте."""
        return f"{working_dir}/{filename}"

    def _log_remote_path(self, working_dir: str, filename: str) -> str:
        """Вычислить путь к файлу лога на целевом хосте."""
        return f"{working_dir}/{filename}.log"

    # ------------------------------------------------------------------
    # Регистрация
    # ------------------------------------------------------------------

    async def register(
        self,
        filename: str,
        local_file_path: Path,
        hostname: str,
        jvm_args: str = "",
        rate_limit: int = 0,
    ) -> MockConfig:
        """
        Зарегистрировать новую заглушку.

        Последовательность:
        1. Проверить уникальность имени файла
        2. Получить конфигурацию целевого хоста
        3. Скопировать артефакт на хост через SCP
        4. Удалить временный файл с сервера управляющего приложения
        5. Создать запись в Redis

        Args:
            filename: Имя файла артефакта (.jar / .war).
            local_file_path: Путь к временному файлу на сервере.
            hostname: Целевой хост из реестра.
            jvm_args: Аргументы JVM.
            rate_limit: Пороговое значение Rate Limiter (0 = без ограничений).

        Returns:
            Созданная конфигурация заглушки.

        Raises:
            LifecycleError: При любой ошибке в процессе регистрации.
        """
        # Проверка уникальности
        if await self._mocks.exists(filename):
            raise LifecycleError(
                f"Mock '{filename}' already registered. "
                "Rename the artifact or delete the existing mock."
            )

        # Получить конфигурацию хоста
        host_config = await self._hosts.get(hostname)
        if host_config is None:
            raise LifecycleError(f"Host '{hostname}' not found in registry")

        # Копирование артефакта на целевой хост
        ssh_client = await self._build_ssh_client(host_config, hostname)
        success = await copy_artifact(
            client=ssh_client,
            local_path=str(local_file_path),
            remote_dir=host_config.working_dir,
            filename=filename,
        )
        if not success:
            raise LifecycleError(
                f"Failed to copy artifact to {hostname}:{host_config.working_dir}"
            )

        # Удалить временный файл
        try:
            os.unlink(local_file_path)
            logger.info("Removed temp file: %s", local_file_path)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", local_file_path, exc)

        # Создать запись в Redis
        config = MockConfig(
            hostname=hostname,
            jvm_args=jvm_args,
            rate_limit=rate_limit,
            rate_limit_enabled=False,
            status=MockStatus.REGISTERED,
            registered_at=datetime.utcnow(),
        )

        registered = await self._mocks.register(filename, config)
        if not registered:
            raise LifecycleError(f"Failed to register mock '{filename}' in Redis")

        logger.info("Registered mock '%s' on host '%s'", filename, hostname)
        return config

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------

    async def start(self, filename: str) -> MockConfig:
        """
        Запустить зарегистрированную заглушку.

        Последовательность:
        1. Прочитать конфигурацию из Redis
        2. Определить свободный порт на целевом хосте
        3. Запустить Java-процесс через SSH (nohup)
        4. Обновить запись в Redis (PID, порт, статус RUNNING)

        Raises:
            LifecycleError: Если заглушка не найдена, уже запущена
                            или произошла ошибка запуска.
        """
        mock_config = await self._mocks.get(filename)
        if mock_config is None:
            raise LifecycleError(f"Mock '{filename}' not found")

        if mock_config.status == MockStatus.RUNNING:
            raise LifecycleError(f"Mock '{filename}' is already running")

        host_config = await self._hosts.get(mock_config.hostname)
        if host_config is None:
            raise LifecycleError(f"Host '{mock_config.hostname}' not found")

        ssh_client = await self._build_ssh_client(host_config, mock_config.hostname)

        # Найти свободный порт
        port = await find_free_port(
            client=ssh_client,
            port_min=host_config.mock_port_min,
            port_max=host_config.mock_port_max,
        )
        if port is None:
            await self._mocks.update_field(filename, status=MockStatus.ERROR)
            raise LifecycleError(
                f"No free ports on {mock_config.hostname} "
                f"in range {host_config.mock_port_min}-{host_config.mock_port_max}"
            )

        # Запуск Java-процесса
        artifact_path = self._artifact_remote_path(host_config.working_dir, filename)
        log_path = self._log_remote_path(host_config.working_dir, filename)

        pid = await start_java_process(
            client=ssh_client,
            java_path=host_config.java_path,
            artifact_path=artifact_path,
            port=port,
            jvm_args=mock_config.jvm_args,
            log_file=log_path,
        )
        if pid is None:
            await self._mocks.update_field(filename, status=MockStatus.ERROR)
            raise LifecycleError(f"Failed to start Java process for '{filename}'")

        # Обновить запись
        updated = await self._mocks.update_field(
            filename,
            port=port,
            pid=pid,
            status=MockStatus.RUNNING,
            started_at=datetime.utcnow(),
        )

        logger.info(
            "Started mock '%s': PID=%d, port=%d on %s",
            filename, pid, port, mock_config.hostname,
        )
        return updated

    # ------------------------------------------------------------------
    # Остановка
    # ------------------------------------------------------------------

    async def stop(self, filename: str) -> MockConfig:
        """
        Остановить запущенную заглушку.

        Отправляет SIGTERM → ожидание 5 сек → SIGKILL.
        Обнуляет PID, порт и started_at в Redis.

        Raises:
            LifecycleError: Если заглушка не найдена или не запущена.
        """
        mock_config = await self._mocks.get(filename)
        if mock_config is None:
            raise LifecycleError(f"Mock '{filename}' not found")

        if mock_config.status != MockStatus.RUNNING or mock_config.pid is None:
            raise LifecycleError(f"Mock '{filename}' is not running")

        host_config = await self._hosts.get(mock_config.hostname)
        if host_config is None:
            raise LifecycleError(f"Host '{mock_config.hostname}' not found")

        ssh_client = await self._build_ssh_client(host_config, mock_config.hostname)
        await stop_process(ssh_client, mock_config.pid)

        updated = await self._mocks.update_field(
            filename,
            status=MockStatus.STOPPED,
            pid=None,
            port=None,
            started_at=None,
        )

        logger.info("Stopped mock '%s' (was PID=%d)", filename, mock_config.pid)
        return updated

    # ------------------------------------------------------------------
    # Удаление
    # ------------------------------------------------------------------

    async def delete(self, filename: str) -> None:
        """
        Удалить заглушку из системы.

        Последовательность:
        1. Остановить процесс (если RUNNING)
        2. Удалить бинарный файл с целевого хоста
        3. Удалить запись из Redis

        Raises:
            LifecycleError: Если заглушка не найдена.
        """
        mock_config = await self._mocks.get(filename)
        if mock_config is None:
            raise LifecycleError(f"Mock '{filename}' not found")

        host_config = await self._hosts.get(mock_config.hostname)

        # Остановить процесс, если запущен
        if mock_config.status == MockStatus.RUNNING and mock_config.pid is not None:
            if host_config is not None:
                try:
                    ssh_client = await self._build_ssh_client(host_config, mock_config.hostname)
                    await stop_process(ssh_client, mock_config.pid)
                except Exception as exc:
                    logger.warning(
                        "Could not stop process for '%s' during deletion: %s",
                        filename, exc,
                    )

        # Удалить файл с хоста
        if host_config is not None:
            try:
                ssh_client = await self._build_ssh_client(host_config, mock_config.hostname)
                artifact_path = self._artifact_remote_path(
                    host_config.working_dir, filename
                )
                log_path = self._log_remote_path(host_config.working_dir, filename)
                await delete_remote_file(ssh_client, artifact_path)
                await delete_remote_file(ssh_client, log_path)
            except Exception as exc:
                logger.warning(
                    "Could not delete remote files for '%s': %s",
                    filename, exc,
                )

        # Удалить из Redis
        await self._mocks.delete(filename)
        logger.info("Deleted mock '%s'", filename)

    # ------------------------------------------------------------------
    # Получение логов
    # ------------------------------------------------------------------

    async def get_logs(self, filename: str, lines: int = 200) -> str:
        """Прочитать последние N строк лога заглушки с целевого хоста."""
        mock_config = await self._mocks.get(filename)
        if mock_config is None:
            raise LifecycleError(f"Mock '{filename}' not found")

        host_config = await self._hosts.get(mock_config.hostname)
        if host_config is None:
            raise LifecycleError(f"Host '{mock_config.hostname}' not found")

        ssh_client = await self._build_ssh_client(host_config, mock_config.hostname)
        log_path = self._log_remote_path(host_config.working_dir, filename)
        return await read_log_tail(ssh_client, log_path, lines)

    # ------------------------------------------------------------------
    # Reconciliation — верификация состояний после перезапуска
    # ------------------------------------------------------------------

    async def reconcile(self) -> dict[str, str]:
        """
        Верифицировать состояния всех заглушек после перезапуска системы.

        Для каждой заглушки со статусом RUNNING проверяет фактическое
        наличие процесса на хосте. Если процесс не найден — обновляет
        статус до STOPPED.

        Returns:
            Словарь {filename: новый_статус} для изменённых записей.
        """
        changes: dict[str, str] = {}
        configs = await self._mocks.list_configs()

        for filename, config in configs.items():
            if config.status != MockStatus.RUNNING or config.pid is None:
                continue

            host_config = await self._hosts.get(config.hostname)
            if host_config is None:
                await self._mocks.update_field(
                    filename,
                    status=MockStatus.ERROR,
                    pid=None,
                    port=None,
                    started_at=None,
                )
                changes[filename] = MockStatus.ERROR
                continue

            try:
                ssh_client = await self._build_ssh_client(host_config, config.hostname)
                alive = await check_process_alive(ssh_client, config.pid)
            except Exception as exc:
                logger.warning(
                    "Cannot verify mock '%s' on %s: %s",
                    filename, config.hostname, exc,
                )
                alive = False

            if not alive:
                await self._mocks.update_field(
                    filename,
                    status=MockStatus.STOPPED,
                    pid=None,
                    port=None,
                    started_at=None,
                )
                changes[filename] = MockStatus.STOPPED
                logger.info(
                    "Reconciliation: '%s' PID=%d not found, status -> STOPPED",
                    filename, config.pid,
                )

        if changes:
            logger.info("Reconciliation completed: %d changes", len(changes))
        else:
            logger.info("Reconciliation completed: all states consistent")

        return changes
