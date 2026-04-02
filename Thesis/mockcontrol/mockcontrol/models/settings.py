"""
Модель глобальных настроек, хранимых в Redis.

Ключ Redis: ``settings:global``
Тип: String (JSON-сериализация GlobalSettings).

Все настройки имеют значения по умолчанию и применяются
немедленно — без перезапуска приложения:

- rate_limit_window_size    → следующий запрос через прокси
- host_check_interval_sec   → следующий цикл HostChecker
- proxy_timeout_sec         → следующий проксированный запрос
- log_retention_lines       → следующий запрос чтения логов
"""

from pydantic import BaseModel, Field, model_validator


class GlobalSettings(BaseModel):
    """Глобальные настройки системы."""

    rate_limit_window_size: int = Field(
        1,
        ge=1,
        le=60,
        description=(
            "Размер временного окна Rate Limiter (секунды). "
            "Определяет, за какой период подсчитываются запросы."
        ),
    )
    host_check_interval_sec: int = Field(
        30,
        ge=5,
        le=3600,
        description=(
            "Интервал фоновой проверки доступности хостов (секунды). "
            "Слишком малые значения могут создать нагрузку на SSH-серверы."
        ),
    )
    proxy_timeout_sec: int = Field(
        10,
        ge=1,
        le=120,
        description=(
            "Таймаут ожидания ответа от заглушки при проксировании (секунды). "
            "При превышении клиенту возвращается HTTP 504."
        ),
    )
    log_retention_lines: int = Field(
        1000,
        ge=50,
        le=50_000,
        description=(
            "Максимальное число строк лога, запрашиваемых через API. "
            "Ограничивает объём данных, передаваемых по SSH."
        ),
    )

    @model_validator(mode="after")
    def validate_sanity(self) -> "GlobalSettings":
        """Дополнительные проверки взаимной согласованности настроек."""
        if self.proxy_timeout_sec > 60 and self.rate_limit_window_size < 2:
            # Предупреждение: большой таймаут при маленьком окне
            # может привести к неточному подсчёту RPS.
            pass  # Не блокируем, но документируем ограничение
        return self
