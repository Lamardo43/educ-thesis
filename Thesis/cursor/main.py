"""Точка входа MockControl: FastAPI, lifespan, порядок маршрутов (ТЗ «Запуск приложения», «Порядок маршрутов»)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.proxy_router import router as proxy_router
from api.v1.accounts import router as accounts_router
from api.v1.hosts import router as hosts_router
from api.v1.logs import router as logs_api_router
from api.v1.metrics import router as metrics_router
from api.v1.mocks import router as mocks_router
from api.v1.settings import router as settings_api_router
from core.redis_client import get_redis
from dependencies import init_dependencies, shutdown_dependencies
from services.health_checker import run_health_checker
from services.log_collector import run_log_collector
from web.routes import router as web_router

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent


def _configure_quiet_logging() -> None:
    """INFO для приложения и uvicorn.error; без access-log (каждый GET), без httpx/httpcore и без спама asyncssh."""
    logging.getLogger().setLevel(logging.INFO)
    for name in ("uvicorn.access", "httpx", "httpcore", "asyncssh"):
        logging.getLogger(name).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: зависимости, глобальные настройки Redis, восстановление RUNNING, фоновые задачи. Shutdown: отмена задач, закрытие ресурсов."""
    _configure_quiet_logging()
    await init_dependencies(app)

    await app.state.settings_service.get_settings()
    await app.state.lifecycle_manager.restore_state()

    redis = get_redis()
    ssh_pool = app.state.ssh_pool
    crypto = app.state.crypto
    settings = app.state.settings

    app.state.health_checker_task = asyncio.create_task(
        run_health_checker(redis, ssh_pool, crypto, settings),
        name="health_checker",
    )
    app.state.log_collector_task = asyncio.create_task(
        run_log_collector(redis, ssh_pool, crypto, settings),
        name="log_collector",
    )

    yield

    for attr in ("health_checker_task", "log_collector_task"):
        task = getattr(app.state, attr, None)
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await shutdown_dependencies(app)


app = FastAPI(
    title="MockControl",
    version="1.0.0",
    lifespan=lifespan,
)

# Порядок маршрутов — строго по ТЗ: Web → API v1 → /metrics → /static → catch-all прокси последним.
app.include_router(web_router)
app.include_router(mocks_router, prefix="/api/v1/mocks")
app.include_router(hosts_router, prefix="/api/v1/hosts")
app.include_router(accounts_router, prefix="/api/v1/accounts")
app.include_router(settings_api_router, prefix="/api/v1/settings")
app.include_router(logs_api_router, prefix="/api/v1/logs")
app.include_router(metrics_router)
app.mount(
    "/static",
    StaticFiles(directory=str(_BASE_DIR / "web" / "static")),
    name="static",
)
app.include_router(proxy_router)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _configure_quiet_logging()
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=False,
    )
