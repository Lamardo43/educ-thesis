"""Rate Limiter по алгоритму Fixed Window Counter (ТЗ, раздел «Rate Limiter»).

Ключ в Redis: String `rate:{filename}:{window}`, где `{window}` = ⌊unix_time / window_size⌋.
Атомарный INCR; при значении 1 — EXPIRE на `window_size * 2` секунд (защита от утечки ключей).
"""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RateLimiter:
    """Ограничение частоты запросов на заглушку в фиксированных временных окнах."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(self, filename: str, limit: int, window_size: int = 1) -> bool:
        """Инкрементирует счётчик текущего окна и возвращает True, если запрос в пределах лимита."""
        started = time.perf_counter()
        window = int(time.time()) // window_size
        key = f"rate:{filename}:{window}"
        t0 = time.perf_counter()
        count = await self._redis.incr(key)
        incr_ms = (time.perf_counter() - t0) * 1000.0
        expire_ms = 0.0
        if count == 1:
            t0 = time.perf_counter()
            await self._redis.expire(key, window_size * 2)
            expire_ms = (time.perf_counter() - t0) * 1000.0
        total_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "rate_limiter_timing mock=%s key=%s count=%s limit=%s incr_ms=%.2f expire_ms=%.2f total_ms=%.2f",
            filename,
            key,
            count,
            limit,
            incr_ms,
            expire_ms,
            total_ms,
        )
        return count <= limit
