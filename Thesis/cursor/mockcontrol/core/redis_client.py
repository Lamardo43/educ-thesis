"""Асинхронный пул соединений Redis (redis.asyncio)."""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool, Redis

from mockcontrol.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None
_client: Optional[Redis] = None


async def init_redis(redis_url: str | None = None) -> None:
    """
    Создаёт пул соединений и клиент Redis.

    Использует ``REDIS_URL`` из конфигурации (``settings.redis_url``), если
    ``redis_url`` не передан. Выполняет PING при успешном подключении.

    Повторный вызов при уже инициализированном клиенте — no-op.
    """
    global _pool, _client

    if _client is not None:
        return

    url = redis_url if redis_url is not None else settings.redis_url
    pool = ConnectionPool.from_url(url, decode_responses=True)
    client = aioredis.Redis(connection_pool=pool)

    try:
        await client.ping()
    except Exception:
        await client.aclose()
        await pool.aclose()
        raise

    _pool = pool
    _client = client
    logger.info("Redis pool initialized (%s)", url)


async def get_redis() -> Redis:
    """
    Возвращает async-клиент Redis.

    Предназначен для использования с ``Depends(get_redis)`` в FastAPI.
    """
    if _client is None:
        raise RuntimeError("Redis is not initialized; call init_redis() during application startup")
    return _client


async def redis_ping() -> bool:
    """
    Проверяет доступность Redis (PING).

    Возвращает ``True``, если соединение есть и ответ положительный;
    ``False``, если клиент не инициализирован или запрос не удался
    (индикатор «Redis: подключён / отключён» в веб-интерфейсе).
    """
    if _client is None:
        return False
    try:
        return await _client.ping() is True
    except Exception as exc:
        logger.debug("Redis ping failed: %s", exc)
        return False


async def close_redis() -> None:
    """Закрывает клиент и освобождает пул соединений."""
    global _pool, _client

    if _client is not None:
        await _client.aclose()
        _client = None

    if _pool is not None:
        await _pool.aclose()
        _pool = None

    logger.info("Redis connection pool closed")
