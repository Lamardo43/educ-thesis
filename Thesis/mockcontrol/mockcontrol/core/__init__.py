"""
Ядро бизнес-логики MockControl.

Модули:
    crypto          — шифрование/дешифрование паролей SSH (Fernet AES-128)
    rate_limiter    — Fixed Window Counter с атомарным Lua-скриптом
    lifecycle       — управление жизненным циклом заглушек
    proxy           — прозрачный обратный прокси-сервер (httpx)
    metrics         — экспорт метрик в Prometheus exposition format
    host_checker    — фоновая проверка доступности хостов (SSH)
"""

from mockcontrol.core.crypto import CryptoService, CryptoServiceError
from mockcontrol.core.host_checker import HostChecker
from mockcontrol.core.lifecycle import LifecycleError, LifecycleManager
from mockcontrol.core.metrics import MetricsExporter
from mockcontrol.core.proxy import ProxyEngine
from mockcontrol.core.rate_limiter import RateLimiter, RateLimitResult

__all__ = [
    "CryptoService",
    "CryptoServiceError",
    "HostChecker",
    "LifecycleError",
    "LifecycleManager",
    "MetricsExporter",
    "ProxyEngine",
    "RateLimiter",
    "RateLimitResult",
]
