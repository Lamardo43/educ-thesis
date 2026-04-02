"""
Сервисный слой для операций с заглушками в Redis.

Инкапсулирует все GET/SET/DEL/SADD/SREM операции,
предоставляя бизнес-логике типизированный интерфейс.

Ключевые особенности:
- Pipeline для массовых операций чтения (list_configs)
- Оптимистичная блокировка WATCH/MULTI/EXEC (update_field)
- Методы агрегации для Dashboard (count_by_status)
"""

import logging
from collections import Counter
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import WatchError

from mockcontrol.models.mock import MockConfig, MockStatus

logger = logging.getLogger(__name__)

REGISTRY_KEY = "mocks:registry"
MAX_UPDATE_RETRIES = 5


def _mock_key(filename: str) -> str:
    """Ключ Redis для записи заглушки."""
    return f"mocks:{filename}"


class MockService:
    """
    Операции чтения/записи записей заглушек в Redis.

    Является единственной точкой доступа к данным заглушек
    для всех компонентов системы (Lifecycle Manager, Proxy Engine,
    Metrics Exporter, API-роутеры).
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get(self, filename: str) -> Optional[MockConfig]:
        """Получить конфигурацию заглушки по имени файла."""
        raw = await self._redis.get(_mock_key(filename))
        if raw is None:
            return None
        return MockConfig.model_validate_json(raw)

    async def set(self, filename: str, config: MockConfig) -> None:
        """Сохранить или обновить конфигурацию заглушки."""
        await self._redis.set(
            _mock_key(filename),
            config.model_dump_json(),
        )

    async def delete(self, filename: str) -> None:
        """
        Удалить запись заглушки и убрать из реестра.

        Также удаляет связанные ключи Rate Limiter
        (счётчики rate:{filename}:*).
        """
        pipe = self._redis.pipeline(transaction=False)
        pipe.delete(_mock_key(filename))
        pipe.srem(REGISTRY_KEY, filename)
        await pipe.execute()

        # Очистка счётчиков Rate Limiter
        await self._cleanup_rate_keys(filename)

    async def register(self, filename: str, config: MockConfig) -> bool:
        """
        Зарегистрировать новую заглушку.

        SADD возвращает 0, если элемент уже существует.
        В этом случае запись в Redis НЕ создаётся.

        Returns:
            True  — успешная регистрация.
            False — заглушка с таким именем уже существует.
        """
        added = await self._redis.sadd(REGISTRY_KEY, filename)
        if added == 0:
            return False
        await self.set(filename, config)
        logger.debug("Registered mock in Redis: %s", filename)
        return True

    async def exists(self, filename: str) -> bool:
        """Проверить, зарегистрирована ли заглушка (O(1) через SISMEMBER)."""
        return await self._redis.sismember(REGISTRY_KEY, filename)

    # ------------------------------------------------------------------
    # Массовое чтение
    # ------------------------------------------------------------------

    async def list_all(self) -> list[str]:
        """Получить отсортированный список имён всех заглушек."""
        members = await self._redis.smembers(REGISTRY_KEY)
        return sorted(members)

    async def list_configs(self) -> dict[str, MockConfig]:
        """
        Получить все конфигурации заглушек одним Pipeline.

        Сначала читается реестр (SMEMBERS), затем все записи
        считываются одним пакетом (Pipeline без транзакции),
        сокращая число сетевых round-trip до O(1).
        """
        filenames = await self.list_all()
        if not filenames:
            return {}

        pipe = self._redis.pipeline(transaction=False)
        for fn in filenames:
            pipe.get(_mock_key(fn))
        results = await pipe.execute()

        configs: dict[str, MockConfig] = {}
        for fn, raw in zip(filenames, results):
            if raw is not None:
                try:
                    configs[fn] = MockConfig.model_validate_json(raw)
                except Exception as exc:
                    logger.warning("Corrupted mock record '%s': %s", fn, exc)
        return configs

    # ------------------------------------------------------------------
    # Атомарное обновление полей
    # ------------------------------------------------------------------

    async def update_field(self, filename: str, **fields) -> Optional[MockConfig]:
        """
        Атомарное обновление полей заглушки через WATCH/MULTI/EXEC.

        Паттерн оптимистичной блокировки:
        1. WATCH — подписаться на изменения ключа.
        2. GET   — прочитать текущее значение.
        3. Модифицировать объект в памяти Python.
        4. MULTI/SET/EXEC — записать, только если ключ не изменился.
        5. При WatchError — повторить (до MAX_UPDATE_RETRIES раз).

        Args:
            filename: Имя файла заглушки.
            **fields: Поля для обновления (ключ=значение).

        Returns:
            Обновлённая конфигурация или None, если заглушка не найдена.

        Raises:
            RuntimeError: Если не удалось обновить за MAX_UPDATE_RETRIES попыток.
        """
        key = _mock_key(filename)

        for attempt in range(1, MAX_UPDATE_RETRIES + 1):
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        await pipe.unwatch()
                        return None

                    config = MockConfig.model_validate_json(raw)
                    updated = config.model_copy(update=fields)

                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    await pipe.execute()
                    return updated

            except WatchError:
                logger.debug(
                    "WatchError on mock '%s', attempt %d/%d",
                    filename, attempt, MAX_UPDATE_RETRIES,
                )
                continue

        raise RuntimeError(
            f"Failed to update mock '{filename}' after "
            f"{MAX_UPDATE_RETRIES} attempts (concurrent modification)"
        )

    # ------------------------------------------------------------------
    # Агрегация и фильтрация
    # ------------------------------------------------------------------

    async def count_by_status(self) -> Counter[str]:
        """
        Подсчитать заглушки по статусам.

        Используется Dashboard для карточек сводной статистики
        и Metrics Exporter для mockcontrol_mocks_total.
        """
        configs = await self.list_configs()
        counter: Counter[str] = Counter()
        for config in configs.values():
            counter[config.status.value] += 1
        return counter

    async def find_by_hostname(self, hostname: str) -> dict[str, MockConfig]:
        """
        Найти все заглушки, размещённые на заданном хосте.

        Используется при удалении хоста для проверки зависимостей.
        """
        all_configs = await self.list_configs()
        return {
            fn: cfg for fn, cfg in all_configs.items()
            if cfg.hostname == hostname
        }

    async def count_running(self) -> int:
        """Подсчитать количество запущенных заглушек."""
        counts = await self.count_by_status()
        return counts.get(MockStatus.RUNNING.value, 0)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _cleanup_rate_keys(self, filename: str) -> int:
        """Удалить все ключи Rate Limiter для заглушки."""
        pattern = f"rate:{filename}:*"
        deleted_total = 0
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            if keys:
                deleted = await self._redis.delete(*keys)
                deleted_total += deleted
            if cursor == 0:
                break
        if deleted_total:
            logger.debug(
                "Cleaned up %d rate-limit keys for '%s'",
                deleted_total, filename,
            )
        return deleted_total
