"""Rate Limiter по алгоритму Fixed Window Counter (ТЗ, раздел «Rate Limiter»).

Ключ в Redis: String `rate:{filename}:{window}`, где `{window}` = ⌊unix_time / window_size⌋.
Атомарный INCR; при значении 1 — EXPIRE на `window_size * 2` секунд (защита от утечки ключей).
"""

from __future__ import annotations

import time

from redis.asyncio import Redis


class RateLimiter:
    """Ограничение частоты запросов на заглушку в фиксированных временных окнах."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(self, filename: str, limit: int, window_size: int = 1) -> bool:
        """Инкрементирует счётчик текущего окна и возвращает True, если запрос в пределах лимита."""
        window = int(time.time()) // window_size
        key = f"rate:{filename}:{window}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, window_size * 2)
        return count <= limit
