"""
Сервисный слой для операций с учётными записями SSH в Redis.

Инкапсулирует все Redis-операции над записями accounts:{uuid}
и реестром accounts:registry.

Особенность: AccountService НЕ знает о шифровании —
CryptoService вызывается на уровне выше (API-роутер или
LifecycleManager). Сервис работает с уже зашифрованными паролями.
"""

import logging
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import WatchError

from mockcontrol.models.account import AccountConfig

logger = logging.getLogger(__name__)

REGISTRY_KEY = "accounts:registry"
MAX_UPDATE_RETRIES = 5


def _account_key(uuid: str) -> str:
    """Ключ Redis для записи учётной записи."""
    return f"accounts:{uuid}"


class AccountService:
    """
    Операции чтения/записи учётных записей в Redis.

    Является единственной точкой доступа к данным учётных записей.
    Используется LifecycleManager (через HostConfig.account_uuid),
    HostChecker и API-роутером accounts.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get(self, uuid: str) -> Optional[AccountConfig]:
        """Получить учётную запись по UUID."""
        raw = await self._redis.get(_account_key(uuid))
        if raw is None:
            return None
        return AccountConfig.model_validate_json(raw)

    async def set(self, uuid: str, config: AccountConfig) -> None:
        """Сохранить или обновить учётную запись."""
        await self._redis.set(
            _account_key(uuid),
            config.model_dump_json(),
        )

    async def delete(self, uuid: str) -> None:
        """Удалить учётную запись и убрать из реестра."""
        pipe = self._redis.pipeline(transaction=False)
        pipe.delete(_account_key(uuid))
        pipe.srem(REGISTRY_KEY, uuid)
        await pipe.execute()
        logger.debug("Deleted account from Redis: %s", uuid)

    async def register(self, uuid: str, config: AccountConfig) -> bool:
        """
        Зарегистрировать новую учётную запись.

        Returns:
            True — успешная регистрация.
            False — запись с таким UUID уже существует.
        """
        added = await self._redis.sadd(REGISTRY_KEY, uuid)
        if added == 0:
            return False
        await self.set(uuid, config)
        logger.debug("Registered account in Redis: %s", uuid)
        return True

    async def exists(self, uuid: str) -> bool:
        """Проверить, зарегистрирована ли учётная запись."""
        return await self._redis.sismember(REGISTRY_KEY, uuid)

    # ------------------------------------------------------------------
    # Массовое чтение
    # ------------------------------------------------------------------

    async def list_all(self) -> list[str]:
        """Получить отсортированный список UUID всех учётных записей."""
        members = await self._redis.smembers(REGISTRY_KEY)
        return sorted(members)

    async def list_configs(self) -> dict[str, AccountConfig]:
        """Получить все учётные записи одним Pipeline."""
        uuids = await self.list_all()
        if not uuids:
            return {}

        pipe = self._redis.pipeline(transaction=False)
        for u in uuids:
            pipe.get(_account_key(u))
        results = await pipe.execute()

        configs: dict[str, AccountConfig] = {}
        for u, raw in zip(uuids, results):
            if raw is not None:
                try:
                    configs[u] = AccountConfig.model_validate_json(raw)
                except Exception as exc:
                    logger.warning("Corrupted account record '%s': %s", u, exc)
        return configs

    # ------------------------------------------------------------------
    # Атомарное обновление
    # ------------------------------------------------------------------

    async def update_field(self, uuid: str, **fields) -> Optional[AccountConfig]:
        """
        Атомарное обновление полей учётной записи.

        Используется API-роутером для обновления username,
        password_enc и description.

        Args:
            uuid: Идентификатор учётной записи.
            **fields: Поля для обновления.

        Returns:
            Обновлённая конфигурация или None, если запись не найдена.
        """
        key = _account_key(uuid)

        for attempt in range(1, MAX_UPDATE_RETRIES + 1):
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        await pipe.unwatch()
                        return None

                    config = AccountConfig.model_validate_json(raw)
                    updated = config.model_copy(update=fields)

                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    await pipe.execute()
                    return updated

            except WatchError:
                logger.debug(
                    "WatchError on account '%s', attempt %d/%d",
                    uuid, attempt, MAX_UPDATE_RETRIES,
                )
                continue

        raise RuntimeError(
            f"Failed to update account '{uuid}' after "
            f"{MAX_UPDATE_RETRIES} attempts"
        )

    # ------------------------------------------------------------------
    # Проверка использования
    # ------------------------------------------------------------------

    async def count_total(self) -> int:
        """Общее число зарегистрированных учётных записей."""
        return await self._redis.scard(REGISTRY_KEY)
