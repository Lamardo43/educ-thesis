"""
Версионированный API v1.

Собирает все роутеры ресурсов в единый префикс /api/v1.
"""

from fastapi import APIRouter

from mockcontrol.api.v1.accounts import router as accounts_router
from mockcontrol.api.v1.hosts import router as hosts_router
from mockcontrol.api.v1.logs import router as logs_router
from mockcontrol.api.v1.mocks import router as mocks_router
from mockcontrol.api.v1.settings import router as settings_router

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(mocks_router)
v1_router.include_router(hosts_router)
v1_router.include_router(accounts_router)
v1_router.include_router(settings_router)
v1_router.include_router(logs_router)
