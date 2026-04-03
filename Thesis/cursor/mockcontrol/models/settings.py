from pydantic import BaseModel, Field


class GlobalSettings(BaseModel):
    """Global settings hash settings:global in Redis."""

    rate_limit_window_size: int = Field(default=1, ge=1)
    host_check_interval_sec: int = Field(default=30, ge=1)
    proxy_timeout_sec: int = Field(default=10, ge=1)
    log_retention_lines: int = Field(default=1000, ge=1)
