"""Глобальная конфигурация приложения (Pydantic BaseSettings, загрузка из `.env`)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки MockControl из переменных окружения и файла `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="URL подключения к Redis",
        validation_alias="REDIS_URL",
    )
    fernet_key_path: Path = Field(
        default=Path("/etc/mockcontrol/fernet.key"),
        description="Путь к файлу ключа шифрования Fernet",
        validation_alias="FERNET_KEY_PATH",
    )
    temp_upload_dir: Path = Field(
        default=Path("/tmp/mockcontrol"),
        description="Каталог для временных загрузок",
        validation_alias="TEMP_UPLOAD_DIR",
    )
    default_rate_limit_window: int = Field(
        default=1,
        ge=1,
        description="Размер окна Rate Limiter (сек)",
        validation_alias="DEFAULT_RATE_LIMIT_WINDOW",
    )
    default_host_check_interval: int = Field(
        default=30,
        ge=1,
        description="Интервал фоновой проверки хостов (сек)",
        validation_alias="DEFAULT_HOST_CHECK_INTERVAL",
    )
    default_proxy_timeout: int = Field(
        default=10,
        ge=1,
        description="Таймаут проксирования HTTP (сек)",
        validation_alias="DEFAULT_PROXY_TIMEOUT",
    )
    default_log_retention_lines: int = Field(
        default=1000,
        ge=1,
        description="Максимум строк лога в буфере Redis",
        validation_alias="DEFAULT_LOG_RETENTION_LINES",
    )


settings = Settings()
