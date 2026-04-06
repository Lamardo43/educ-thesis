"""Глобальные настройки (Hash settings:global)."""

from typing import Optional

from pydantic import BaseModel, Field


class GlobalSettings(BaseModel):
    """Поля settings:global с значениями по умолчанию из ТЗ."""

    rate_limit_window_size: int = Field(default=1, ge=1, description="Размер окна Rate Limiter (сек)")
    host_check_interval_sec: int = Field(default=30, ge=1, description="Интервал проверки хостов (сек)")
    proxy_timeout_sec: int = Field(default=10, ge=1, description="Таймаут проксирования (сек)")
    log_retention_lines: int = Field(default=1000, ge=1, description="Макс. строк лога в буфере")
    enable_health_checker: bool = Field(
        default=True,
        description="Включение фоновой задачи health checker",
    )
    enable_log_collector: bool = Field(
        default=True,
        description="Включение фоновой задачи log collector",
    )


class GlobalSettingsUpdate(BaseModel):
    """Частичное обновление глобальных настроек."""

    rate_limit_window_size: Optional[int] = Field(default=None, ge=1)
    host_check_interval_sec: Optional[int] = Field(default=None, ge=1)
    proxy_timeout_sec: Optional[int] = Field(default=None, ge=1)
    log_retention_lines: Optional[int] = Field(default=None, ge=1)
    enable_health_checker: Optional[bool] = Field(default=None)
    enable_log_collector: Optional[bool] = Field(default=None)
