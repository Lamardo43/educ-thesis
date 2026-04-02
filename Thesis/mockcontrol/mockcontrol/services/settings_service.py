"""
Сервисный слой для глобальных настроек системы в Redis.

Глобальные настройки хранятся в единственном ключе ``settings:global``
и не требуют реестра (Set), в отличие от заглушек, хостов и учётных записей.

Настройки гарантированно существуют: при первом обращении
создаётся запись со значениями по умолчанию (ensure_defaults).
"""

import logging
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import WatchError

from mockcontrol.models.settings import GlobalSettings

logger = logging.getLogger(__name__)

SETTINGS_KEY = "settings:global"
MAX_UPDATE_RETRIES = 5


class SettingsService:
    """
    Чтение/запись глобальных настроек.

    Используется:
    - ProxyEngine      — для proxy_timeout_sec и rate_limit_window_size
    - HostChecker      — для host_check_interval_sec
    - LifecycleManager — для log_retention_lines
    - API-роутер       — для GET/PUT настроек
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self) -> GlobalSettings:
        """
        Получить глобальные настройки.

        Если запись в Redis отсутствует — возвращает значения
        по умолчанию (но НЕ создаёт запись; для этого —
        ensure_defaults).
        """
        raw = await self._redis.get(SETTINGS_KEY)
        if raw is None:
            return GlobalSettings()
        return GlobalSettings.model_validate_json(raw)

    async def set(self, config: GlobalSettings) -> None:
        """Полная перезапись глобальных настроек."""
        await self._redis.set(SETTINGS_KEY, config.model_dump_json())
        logger.info("Global settings updated")

    async def ensure_defaults(self) -> GlobalSettings:
        """
        Создать запись со значениями по умолчанию, если отсутствует.

        Вызывается при инициализации приложения (lifespan startup).
        Если запись уже существует — возвращает текущие значения
        без изменений.

        Использует SETNX-семантику (SET ... NX) для атомарности.
        """
        defaults = GlobalSettings()
        created = await self._redis.set(
            SETTINGS_KEY,
            defaults.model_dump_json(),
            nx=True,  # Только если ключ не существует
        )
        if created:
            logger.info("Created default global settings")
            return defaults

        # Ключ уже существовал — вернуть текущие значения
        return await self.get()

    async def partial_update(self, **fields) -> GlobalSettings:
        """
        Частичное обновление настроек через WATCH/MULTI/EXEC.

        Позволяет изменить отдельные параметры, сохраняя остальные.

        Args:
            **fields: Пары ключ=значение для обновления.

        Returns:
            Обновлённые настройки.
        """
        for attempt in range(1, MAX_UPDATE_RETRIES + 1):
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    await pipe.watch(SETTINGS_KEY)
                    raw = await pipe.get(SETTINGS_KEY)

                    if raw is None:
                        current = GlobalSettings()
                    else:
                        current = GlobalSettings.model_validate_json(raw)

                    updated = current.model_copy(update=fields)

                    pipe.multi()
                    pipe.set(SETTINGS_KEY, updated.model_dump_json())
                    await pipe.execute()

                    logger.info("Global settings partially updated: %s", list(fields.keys()))
                    return updated

            except WatchError:
                logger.debug(
                    "WatchError on settings, attempt %d/%d",
                    attempt, MAX_UPDATE_RETRIES,
                )
                continue

        raise RuntimeError(
            f"Failed to update settings after {MAX_UPDATE_RETRIES} attempts"
        )

    async def exists(self) -> bool:
        """Проверить, существует ли запись настроек в Redis."""
        return await self._redis.exists(SETTINGS_KEY) > 0
