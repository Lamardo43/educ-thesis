"""
Тесты компонента Rate Limiter.

Проверяет:
- Корректность подсчёта запросов в рамках одного окна
- Отклонение запросов при превышении лимита
- Автоматическую очистку счётчиков (TTL)
- Сброс счётчиков через reset()
- Получение текущего значения без инкремента
"""

import pytest

from mockcontrol.core.rate_limiter import RateLimiter, RateLimitResult


@pytest.mark.asyncio
class TestRateLimiterCheck:
    """Тесты метода check() — основной алгоритм Fixed Window Counter."""

    async def test_allows_requests_within_limit(self, rate_limiter: RateLimiter):
        """Запросы в пределах лимита должны быть разрешены."""
        limit = 5
        for i in range(1, limit + 1):
            result = await rate_limiter.check("test-stub.jar", limit=limit, window_size=60)
            assert result.allowed is True
            assert result.current_count == i
            assert result.limit == limit

    async def test_rejects_requests_exceeding_limit(self, rate_limiter: RateLimiter):
        """Запросы сверх лимита должны быть отклонены."""
        limit = 3
        # Выбрать все разрешённые
        for _ in range(limit):
            await rate_limiter.check("excess-stub.jar", limit=limit, window_size=60)

        # Следующий запрос — отклонён
        result = await rate_limiter.check("excess-stub.jar", limit=limit, window_size=60)
        assert result.allowed is False
        assert result.current_count == limit + 1

    async def test_independent_counters_per_mock(self, rate_limiter: RateLimiter):
        """Каждая заглушка имеет независимый счётчик."""
        await rate_limiter.check("mock-a.jar", limit=1, window_size=60)
        result_b = await rate_limiter.check("mock-b.jar", limit=1, window_size=60)

        # mock-b не затронута лимитом mock-a
        assert result_b.allowed is True
        assert result_b.current_count == 1

    async def test_zero_limit_rejects_all(self, rate_limiter: RateLimiter):
        """Лимит 0 означает отклонение всех запросов (если Rate Limiter включён)."""
        result = await rate_limiter.check("zero-stub.jar", limit=0, window_size=60)
        assert result.allowed is False

    async def test_result_repr(self, rate_limiter: RateLimiter):
        """RateLimitResult имеет читаемое строковое представление."""
        result = await rate_limiter.check("repr-stub.jar", limit=100, window_size=60)
        repr_str = repr(result)
        assert "allowed=True" in repr_str
        assert "count=1/100" in repr_str


@pytest.mark.asyncio
class TestRateLimiterGetCount:
    """Тесты метода get_current_count() — чтение без инкремента."""

    async def test_returns_zero_for_unknown_mock(self, rate_limiter: RateLimiter):
        """Для неизвестной заглушки счётчик равен 0."""
        count = await rate_limiter.get_current_count("unknown.jar", window_size=60)
        assert count == 0

    async def test_returns_current_value_after_checks(self, rate_limiter: RateLimiter):
        """Возвращает текущее значение без его увеличения."""
        await rate_limiter.check("count-stub.jar", limit=100, window_size=60)
        await rate_limiter.check("count-stub.jar", limit=100, window_size=60)

        count = await rate_limiter.get_current_count("count-stub.jar", window_size=60)
        assert count == 2

        # Повторный вызов не увеличивает счётчик
        count2 = await rate_limiter.get_current_count("count-stub.jar", window_size=60)
        assert count2 == 2


@pytest.mark.asyncio
class TestRateLimiterReset:
    """Тесты метода reset() — очистка счётчиков."""

    async def test_reset_clears_counters(self, rate_limiter: RateLimiter):
        """Reset удаляет все ключи rate:{filename}:*."""
        # Накопить счётчик
        for _ in range(5):
            await rate_limiter.check("reset-stub.jar", limit=100, window_size=60)

        # Сбросить
        await rate_limiter.reset("reset-stub.jar")

        # Счётчик должен быть 0
        count = await rate_limiter.get_current_count("reset-stub.jar", window_size=60)
        assert count == 0

    async def test_reset_does_not_affect_other_mocks(self, rate_limiter: RateLimiter):
        """Reset одной заглушки не затрагивает другие."""
        await rate_limiter.check("keep-stub.jar", limit=100, window_size=60)
        await rate_limiter.check("clear-stub.jar", limit=100, window_size=60)

        await rate_limiter.reset("clear-stub.jar")

        keep_count = await rate_limiter.get_current_count("keep-stub.jar", window_size=60)
        assert keep_count == 1
