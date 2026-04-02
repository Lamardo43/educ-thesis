"""
Rate Limiter — алгоритм «Счётчик с фиксированным временным окном».

Реализация использует атомарный Lua-скрипт в Redis, что устраняет
race condition между INCR и EXPIRE, возможный при последовательном
вызове этих команд.

Каждая заглушка имеет независимый счётчик. Ключ Redis формируется как:
    rate:{filename}:{window}

где window = floor(unix_timestamp / window_size).

TTL ключа устанавливается равным удвоенному размеру окна для
гарантированного самоочищения при расхождениях системного времени.
"""

import logging
import time
from math import floor
from typing import Optional

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Lua-скрипт: атомарный инкремент + установка TTL при первом вызове в окне.
# KEYS[1] — ключ счётчика
# ARGV[1] — TTL в секундах (= window_size * 2)
# Возвращает текущее значение счётчика.
_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class RateLimitResult:
    """Результат проверки Rate Limit."""

    __slots__ = ("allowed", "current_count", "limit")

    def __init__(self, allowed: bool, current_count: int, limit: int) -> None:
        self.allowed = allowed
        self.current_count = current_count
        self.limit = limit

    def __repr__(self) -> str:
        return (
            f"RateLimitResult(allowed={self.allowed}, "
            f"count={self.current_count}/{self.limit})"
        )


class RateLimiter:
    """
    Контроль интенсивности запросов для каждой заглушки.

    Реализует паттерн Fixed Window Counter с использованием
    атомарного Lua-скрипта в Redis.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script: Optional[object] = None

    async def _ensure_script(self) -> None:
        """Зарегистрировать Lua-скрипт в Redis (однократно)."""
        if self._script is None:
            self._script = self._redis.register_script(_RATE_LIMIT_LUA)

    async def check(
        self,
        filename: str,
        limit: int,
        window_size: int = 1,
    ) -> RateLimitResult:
        """
        Проверить, допускается ли следующий запрос к заглушке.

        Args:
            filename: Имя файла заглушки (уникальный идентификатор).
            limit: Максимально допустимое число запросов в окне.
            window_size: Размер временного окна в секундах.

        Returns:
            RateLimitResult с флагом допуска и текущим счётчиком.
        """
        await self._ensure_script()

        # Вычисление метки текущего окна
        now = time.time()
        window = floor(now / window_size)
        key = f"rate:{filename}:{window}"

        # TTL = 2 × window_size — запас на расхождения времени
        ttl = window_size * 2

        # Атомарное выполнение Lua-скрипта
        current_count = await self._script(keys=[key], args=[ttl])

        allowed = current_count <= limit
        if not allowed:
            logger.debug(
                "Rate limit exceeded for %s: %d/%d (window=%d)",
                filename, current_count, limit, window,
            )

        return RateLimitResult(
            allowed=allowed,
            current_count=current_count,
            limit=limit,
        )

    async def get_current_count(
        self,
        filename: str,
        window_size: int = 1,
    ) -> int:
        """
        Получить текущее значение счётчика без инкремента.

        Используется Metrics Exporter для отображения текущего RPS.
        """
        now = time.time()
        window = floor(now / window_size)
        key = f"rate:{filename}:{window}"

        value = await self._redis.get(key)
        if value is None:
            return 0
        return int(value)

    async def reset(self, filename: str) -> None:
        """
        Сбросить все счётчики для заглушки.

        Удаляет ключи rate:{filename}:* — применяется при отключении
        Rate Limiter или удалении заглушки.
        """
        pattern = f"rate:{filename}:*"
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor,
                match=pattern,
                count=100,
            )
            if keys:
                await self._redis.delete(*keys)
            if cursor == 0:
                break
