from pydantic import BaseModel


class GlobalSettings(BaseModel):
    rate_limit_window_size: int = 1       # seconds
    host_check_interval_sec: int = 30
    proxy_timeout_sec: int = 10
    log_retention_lines: int = 1000
