"""Асинхронный клиент Redis на базе пула соединений (`redis.asyncio`).

Данные приложения (см. ТЗ, раздел «Хранение данных в Redis»):
- заглушки: Hash `mocks:{filename}`, реестр Set `mocks:registry`;
- хосты: Hash `hosts:{hostname}`, реестр `hosts:registry`;
- учётные записи: Hash `accounts:{uuid}`, реестр `accounts:registry`;
- rate limit: String `rate:{filename}:{window}` с INCR/TTL;
- глобальные настройки: Hash `settings:global`;
- логи: List `logs:{filename}`;
- метрики: String `metrics:proxy_total:{filename}`, `metrics:rejected_total:{filename}`.

Объекты-заглушки, хосты и учётные записи хранятся как Redis Hash (HSET/HGET/HGETALL),
а не как JSON-строки.
"""

from __future__ import annotations

import logging

from redis.asyncio import ConnectionPool, Redis

from config import Settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


async def init_redis(settings: Settings) -> None:
    """Создаёт пул соединений и глобальный клиент Redis по `settings.redis_url`.

    Повторный вызов закрывает предыдущий клиент и создаёт новый.
    """
    global _pool, _client

    if _client is not None:
        await close_redis()

    _pool = ConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        encoding="utf-8",
    )
    _client = Redis(connection_pool=_pool)
    await _client.ping()
    logger.info("Redis: pool connected (%s)", settings.redis_url)


def get_redis() -> Redis:
    """Возвращает общий async-клиент Redis для внедрения в FastAPI (`Depends(get_redis)`)."""
    if _client is None:
        raise RuntimeError(
            "Redis не инициализирован: вызовите init_redis() при старте приложения."
        )
    return _client


async def ping_redis() -> bool:
    """Проверяет доступность Redis (PING). Для индикатора «Redis: подключён / отключён»."""
    if _client is None:
        return False
    try:
        await _client.ping()
        return True
    except Exception:
        logger.exception("Redis: ping error")
        return False


async def close_redis() -> None:
    """Корректно закрывает клиент и освобождает соединения пула."""
    global _pool, _client

    if _client is not None:
        await _client.aclose()
        _client = None
    _pool = None
    logger.info("Redis: pool closed")
