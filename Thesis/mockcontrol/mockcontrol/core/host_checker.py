"""
Host Checker — периодическая проверка доступности целевых хостов.

Запускается как фоновая asyncio-задача при старте приложения.
С настраиваемым интервалом обходит реестр хостов и проверяет
SSH-соединение к каждому из них. Результат записывается в Redis
(поля status и last_checked_at).

Это позволяет Dashboard отображать актуальный статус доступности
каждого хоста без выполнения оператором диагностических действий.
"""

import asyncio
import logging
from datetime import datetime

from mockcontrol.core.crypto import CryptoService
from mockcontrol.models.host import HostStatus
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.settings_service import SettingsService
from mockcontrol.ssh.client import AsyncSSHClient, SSHCredentials

logger = logging.getLogger(__name__)


class HostChecker:
    """
    Фоновая задача проверки доступности SSH-соединений.

    Для каждого хоста в реестре:
    1. Считывает учётные данные из Redis.
    2. Расшифровывает пароль через CryptoService.
    3. Пробует установить SSH-соединение и выполнить `echo ok`.
    4. Обновляет status (AVAILABLE / UNAVAILABLE) и last_checked_at.
    """

    def __init__(
        self,
        host_service: HostService,
        account_service: AccountService,
        settings_service: SettingsService,
        crypto: CryptoService,
    ) -> None:
        self._hosts = host_service
        self._accounts = account_service
        self._settings = settings_service
        self._crypto = crypto
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Запустить фоновую задачу проверки."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="host-checker")
        logger.info("Host checker started")

    async def stop(self) -> None:
        """Остановить фоновую задачу."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Host checker stopped")

    async def _loop(self) -> None:
        """Основной цикл: проверка → пауза → проверка."""
        while self._running:
            try:
                await self._check_all_hosts()
            except Exception as exc:
                logger.error("Host checker cycle error: %s", exc, exc_info=True)

            # Считываем интервал из глобальных настроек при каждой итерации,
            # чтобы изменения применялись без перезапуска
            try:
                global_settings = await self._settings.get()
                interval = global_settings.host_check_interval_sec
            except Exception:
                interval = 30

            await asyncio.sleep(interval)

    async def _check_all_hosts(self) -> None:
        """Проверить все хосты из реестра."""
        host_configs = await self._hosts.list_configs()
        if not host_configs:
            return

        # Запускаем проверки параллельно для ускорения
        tasks = [
            self._check_single_host(hostname, config)
            for hostname, config in host_configs.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_single_host(self, hostname: str, host_config) -> None:
        """Проверить доступность одного хоста."""
        now = datetime.utcnow()

        # Получить учётные данные
        account = await self._accounts.get(host_config.account_uuid)
        if account is None:
            logger.warning(
                "Account %s not found for host %s, marking UNAVAILABLE",
                host_config.account_uuid, hostname,
            )
            await self._hosts.update_field(
                hostname,
                status=HostStatus.UNAVAILABLE,
                last_checked_at=now,
            )
            return

        try:
            password = self._crypto.decrypt(account.password_enc)
        except Exception as exc:
            logger.error(
                "Cannot decrypt password for host %s: %s",
                hostname, exc,
            )
            await self._hosts.update_field(
                hostname,
                status=HostStatus.UNAVAILABLE,
                last_checked_at=now,
            )
            return

        credentials = SSHCredentials(
            hostname=hostname,
            port=host_config.ssh_port,
            username=account.username,
            password=password,
        )
        client = AsyncSSHClient(credentials, connect_timeout=5.0)

        is_available = await client.check_connectivity()
        new_status = HostStatus.AVAILABLE if is_available else HostStatus.UNAVAILABLE

        # Обновить только если статус изменился или для записи last_checked_at
        await self._hosts.update_field(
            hostname,
            status=new_status,
            last_checked_at=now,
        )

        if new_status == HostStatus.UNAVAILABLE:
            logger.warning("Host %s is UNAVAILABLE", hostname)

    async def check_single(self, hostname: str) -> HostStatus:
        """
        Проверить доступность одного хоста по запросу (вне цикла).

        Используется при регистрации нового хоста для немедленной
        проверки введённых параметров.

        Returns:
            Результат проверки: AVAILABLE или UNAVAILABLE.
        """
        host_config = await self._hosts.get(hostname)
        if host_config is None:
            return HostStatus.UNAVAILABLE

        await self._check_single_host(hostname, host_config)

        updated = await self._hosts.get(hostname)
        return updated.status if updated else HostStatus.UNAVAILABLE
