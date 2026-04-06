"""Глобальная конфигурация приложения (Pydantic Settings, значения из .env)."""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки MockControl из переменных окружения и файла `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="URL подключения к Redis",
    )
    fernet_key_path: str = Field(
        default="/etc/mockcontrol/fernet.key",
        description="Путь к файлу ключа Fernet",
    )
    temp_upload_dir: str = Field(
        default="/tmp/mockcontrol",
        description="Каталог для временных загрузок",
    )
    default_rate_limit_window: int = Field(
        default=1,
        ge=1,
        description="Размер окна Rate Limiter по умолчанию (сек)",
    )
    default_host_check_interval: int = Field(
        default=30,
        ge=1,
        description="Интервал проверки хостов по умолчанию (сек)",
    )
    default_proxy_timeout: int = Field(
        default=10,
        ge=1,
        description="Таймаут прокси по умолчанию (сек)",
    )
    default_log_retention_lines: int = Field(
        default=1000,
        ge=1,
        description="Максимум строк лога в буфере по умолчанию",
    )
    uvicorn_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Число процессов Uvicorn; не задано — CPU×2 (минимум 1)",
    )
