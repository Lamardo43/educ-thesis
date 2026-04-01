"""
Unit tests for the Fixed Window Counter rate limiter.
Redis calls are mocked via unittest.mock.AsyncMock.
"""
import math
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rate_limiter import get_current_rps, is_allowed


def _make_redis(incr_value: int, ttl_value: int = -1):
    """Return a mock Redis client with pre-configured pipeline results."""
    mock_pipe = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__ = AsyncMock(return_value=False)
    mock_pipe.incr = MagicMock()
    mock_pipe.ttl = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=[incr_value, ttl_value])

    r = AsyncMock()
    r.pipeline = MagicMock(return_value=mock_pipe)
    r.expire = AsyncMock(return_value=True)
    r.get = AsyncMock(return_value=None)
    return r, mock_pipe


@pytest.mark.asyncio
async def test_first_request_allowed():
    """First request in a new window should always be allowed."""
    r, _ = _make_redis(incr_value=1, ttl_value=-1)
    result = await is_allowed(r, "test.jar", limit=10)
    assert result is True


@pytest.mark.asyncio
async def test_ttl_set_on_new_key():
    """EXPIRE should be called when the counter is created (TTL == -1)."""
    r, _ = _make_redis(incr_value=1, ttl_value=-1)
    await is_allowed(r, "test.jar", limit=10, window_size=1)
    r.expire.assert_called_once()


@pytest.mark.asyncio
async def test_ttl_not_set_on_existing_key():
    """EXPIRE must NOT be called when the key already has a TTL set."""
    r, _ = _make_redis(incr_value=5, ttl_value=1)
    await is_allowed(r, "test.jar", limit=10)
    r.expire.assert_not_called()


@pytest.mark.asyncio
async def test_within_limit_allowed():
    """Request exactly at the limit should be allowed."""
    r, _ = _make_redis(incr_value=10, ttl_value=1)
    result = await is_allowed(r, "test.jar", limit=10)
    assert result is True


@pytest.mark.asyncio
async def test_over_limit_rejected():
    """Request one over the limit must be rejected."""
    r, _ = _make_redis(incr_value=11, ttl_value=1)
    result = await is_allowed(r, "test.jar", limit=10)
    assert result is False


@pytest.mark.asyncio
async def test_different_mocks_isolated():
    """Each mock gets its own counter key."""
    r1, _ = _make_redis(incr_value=1, ttl_value=-1)
    r2, _ = _make_redis(incr_value=999, ttl_value=1)

    assert await is_allowed(r1, "mock-a.jar", limit=10) is True
    assert await is_allowed(r2, "mock-b.jar", limit=10) is False


@pytest.mark.asyncio
async def test_key_includes_filename_and_window():
    """Verify the Redis key uses the correct mock name and window bucket."""
    r, pipe = _make_redis(incr_value=1, ttl_value=-1)
    window_size = 1
    window = math.floor(time.time() / window_size)
    expected_key = f"rate:payment.jar:{window}"

    await is_allowed(r, "payment.jar", limit=100, window_size=window_size)

    pipe.incr.assert_called_once_with(expected_key)


@pytest.mark.asyncio
async def test_get_current_rps_returns_zero_when_no_key():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    result = await get_current_rps(r, "no-such.jar")
    assert result == 0


@pytest.mark.asyncio
async def test_get_current_rps_returns_count():
    r = AsyncMock()
    r.get = AsyncMock(return_value="42")
    result = await get_current_rps(r, "some.jar")
    assert result == 42
