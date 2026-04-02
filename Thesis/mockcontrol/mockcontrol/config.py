"""
Конфигурация приложения MockControl.

Все параметры считываются из переменных окружения или .env-файла.
Используется pydantic-settings для валидации при старте.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Настройки приложения, считываемые из окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MC_",
    )

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Безопасность ---
    fernet_key_path: Path = Path("fernet.key")

    # --- Временный каталог для загрузки артефактов ---
    tmp_upload_dir: Path = Path("/tmp/mockcontrol_uploads")

    # --- Uvicorn ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Значения по умолчанию для глобальных настроек ---
    default_rate_limit_window: int = 1          # секунды
    default_host_check_interval: int = 30       # секунды
    default_proxy_timeout: int = 10             # секунды
    default_log_retention_lines: int = 1000


settings = AppSettings()
