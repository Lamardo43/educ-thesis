"""
Пакет API — объединяет версионированные роутеры и служебные эндпоинты.
"""

from mockcontrol.api.metrics import router as metrics_router
from mockcontrol.api.v1 import v1_router
from mockcontrol.api.v1.proxy import router as proxy_router

__all__ = [
    "v1_router",
    "metrics_router",
    "proxy_router",
]
