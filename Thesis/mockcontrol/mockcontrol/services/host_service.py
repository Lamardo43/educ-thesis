"""
Сервисный слой для операций с целевыми хостами в Redis.

Инкапсулирует все Redis-операции над записями hosts:{hostname}
и реестром hosts:registry.

Ключевые особенности:
- Pipeline для массового чтения
- Оптимистичная блокировка при обновлении
- Агрегация по статусам для Dashboard
"""

import logging
from collections import Counter
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import WatchError

from mockcontrol.models.host import HostConfig, HostStatus

logger = logging.getLogger(__name__)

REGISTRY_KEY = "hosts:registry"
MAX_UPDATE_RETRIES = 5


def _host_key(hostname: str) -> str:
    """Ключ Redis для записи хоста."""
    return f"hosts:{hostname}"


class HostService:
    """
    Операции чтения/записи записей хостов в Redis.

    Является единственной точкой доступа к данным хостов
    для HostChecker, LifecycleManager и API-роутеров.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get(self, hostname: str) -> Optional[HostConfig]:
        """Получить конфигурацию хоста по hostname (IP или DNS)."""
        raw = await self._redis.get(_host_key(hostname))
        if raw is None:
            return None
        return HostConfig.model_validate_json(raw)

    async def set(self, hostname: str, config: HostConfig) -> None:
        """Сохранить или обновить конфигурацию хоста."""
        await self._redis.set(
            _host_key(hostname),
            config.model_dump_json(),
        )

    async def delete(self, hostname: str) -> None:
        """Удалить запись хоста и убрать из реестра."""
        pipe = self._redis.pipeline(transaction=False)
        pipe.delete(_host_key(hostname))
        pipe.srem(REGISTRY_KEY, hostname)
        await pipe.execute()
        logger.debug("Deleted host from Redis: %s", hostname)

    async def register(self, hostname: str, config: HostConfig) -> bool:
        """
        Зарегистрировать новый хост.

        Returns:
            True — успешная регистрация.
            False — хост уже зарегистрирован.
        """
        added = await self._redis.sadd(REGISTRY_KEY, hostname)
        if added == 0:
            return False
        await self.set(hostname, config)
        logger.debug("Registered host in Redis: %s", hostname)
        return True

    async def exists(self, hostname: str) -> bool:
        """Проверить, зарегистрирован ли хост."""
        return await self._redis.sismember(REGISTRY_KEY, hostname)

    # ------------------------------------------------------------------
    # Массовое чтение
    # ------------------------------------------------------------------

    async def list_all(self) -> list[str]:
        """Получить отсортированный список hostname всех хостов."""
        members = await self._redis.smembers(REGISTRY_KEY)
        return sorted(members)

    async def list_configs(self) -> dict[str, HostConfig]:
        """
        Получить все конфигурации хостов одним Pipeline.

        Аналогично MockService.list_configs — один round-trip к Redis.
        """
        hostnames = await self.list_all()
        if not hostnames:
            return {}

        pipe = self._redis.pipeline(transaction=False)
        for h in hostnames:
            pipe.get(_host_key(h))
        results = await pipe.execute()

        configs: dict[str, HostConfig] = {}
        for h, raw in zip(hostnames, results):
            if raw is not None:
                try:
                    configs[h] = HostConfig.model_validate_json(raw)
                except Exception as exc:
                    logger.warning("Corrupted host record '%s': %s", h, exc)
        return configs

    # ------------------------------------------------------------------
    # Атомарное обновление полей
    # ------------------------------------------------------------------

    async def update_field(self, hostname: str, **fields) -> Optional[HostConfig]:
        """
        Атомарное обновление полей хоста через WATCH/MULTI/EXEC.

        Применяется для обновления status и last_checked_at
        компонентом HostChecker, а также для ручного редактирования
        параметров через API.

        Args:
            hostname: Идентификатор хоста.
            **fields: Поля для обновления.

        Returns:
            Обновлённая конфигурация или None, если хост не найден.
        """
        key = _host_key(hostname)

        for attempt in range(1, MAX_UPDATE_RETRIES + 1):
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        await pipe.unwatch()
                        return None

                    config = HostConfig.model_validate_json(raw)
                    updated = config.model_copy(update=fields)

                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    await pipe.execute()
                    return updated

            except WatchError:
                logger.debug(
                    "WatchError on host '%s', attempt %d/%d",
                    hostname, attempt, MAX_UPDATE_RETRIES,
                )
                continue

        raise RuntimeError(
            f"Failed to update host '{hostname}' after "
            f"{MAX_UPDATE_RETRIES} attempts"
        )

    # ------------------------------------------------------------------
    # Агрегация
    # ------------------------------------------------------------------

    async def count_by_status(self) -> Counter[str]:
        """
        Подсчитать хосты по статусам.

        Используется Dashboard для карточки «Хостов доступно»
        и Metrics Exporter для mockcontrol_hosts_total.
        """
        configs = await self.list_configs()
        counter: Counter[str] = Counter()
        for config in configs.values():
            counter[config.status.value] += 1
        return counter

    async def count_available(self) -> int:
        """Подсчитать число доступных хостов."""
        counts = await self.count_by_status()
        return counts.get(HostStatus.AVAILABLE.value, 0)

    async def count_total(self) -> int:
        """Общее число зарегистрированных хостов."""
        return await self._redis.scard(REGISTRY_KEY)
