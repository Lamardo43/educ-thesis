"""
Fixed Window Counter rate limiter.

Key schema:  rate:{filename}:{window}
  where window = floor(unix_timestamp / window_size_seconds)

TTL is set to 2 * window_size so stale keys are automatically evicted.
"""
import math
import time

import redis.asyncio as aioredis


async def is_allowed(
    r: aioredis.Redis,
    filename: str,
    limit: int,
    window_size: int = 1,
) -> bool:
    """
    Increment the counter for the current window and return True if the
    request is within the allowed limit, False if it should be rejected (HTTP 429).
    """
    window = math.floor(time.time() / window_size)
    key = f"rate:{filename}:{window}"

    pipe = r.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    count, ttl = await pipe.execute()

    # Set TTL only when the key is brand-new (first request in the window)
    if ttl == -1:  # -1 means key exists but has no TTL
        await r.expire(key, window_size * 2)

    return count <= limit


async def get_current_rps(
    r: aioredis.Redis,
    filename: str,
    window_size: int = 1,
) -> int:
    """Return the request count in the current window (approximates current RPS)."""
    window = math.floor(time.time() / window_size)
    key = f"rate:{filename}:{window}"
    val = await r.get(key)
    return int(val) if val else 0
