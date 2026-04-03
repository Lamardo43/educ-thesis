"""Fixed Window Counter rate limiting через Redis."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

_FIXED_WINDOW_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""


class RateLimiter:
    """Ограничение частоты по фиксированным окнам (счётчик в Redis)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._incr_and_expire = self._redis.register_script(_FIXED_WINDOW_SCRIPT)

    async def check(self, filename: str, limit: int, window_size: int = 1) -> bool:
        """
        Увеличивает счётчик в текущем окне и возвращает, не превышен ли лимит.

        Ключ: ``rate:{filename}:{window}``, где ``window = floor(unix_time / window_size)``.
        После первого обращения к ключу выставляется TTL ``window_size * 2`` секунд.
        """
        window = int(time.time()) // window_size
        key = f"rate:{filename}:{window}"
        expire_seconds = window_size * 2
        count = await self._incr_and_expire(keys=[key], args=[expire_seconds])
        return int(count) <= limit
