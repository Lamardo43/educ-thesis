"""
Mock Control — FastAPI application entry point.

Router registration ORDER matters:
  1. /metrics          (plain text, no conflict)
  2. /api/v1/...       (REST API)
  3. /                 (SSR web pages)
  4. /{mock_name}/...  (catch-all proxy — MUST be last)

Redis lifecycle:
  EmbeddedRedisManager запускается в lifespan — если Redis ещё
  не доступен, поднимается собственный процесс redis-server.
  При остановке приложения Redis корректно завершается.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.redis_client import close_redis, get_redis
from app.core.redis_server import EmbeddedRedisManager
from app.services.host_checker import host_checker_loop
from app.services.localhost_setup import ensure_localhost_host
from app.services.startup_reconciler import reconcile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_checker_task: asyncio.Task | None = None
_redis_manager = EmbeddedRedisManager(
    host=settings.redis_host,
    port=settings.redis_port,
    data_dir=settings.redis_data_dir,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _checker_task

    # 1. Поднять Redis (если не запущен извне)
    await _redis_manager.start()

    # 2. Подключиться и проверить
    r = await get_redis()
    await r.ping()
    logger.info("Redis connected at %s:%d", settings.redis_host, settings.redis_port)

    # 3. Зарегистрировать localhost-хост если его ещё нет
    await ensure_localhost_host(r)

    # 3. Reconcile RUNNING mocks после возможного перезапуска
    await reconcile(r)

    # 4. Запустить фоновую проверку SSH-доступности хостов
    _checker_task = asyncio.create_task(host_checker_loop(r))
    logger.info("MockControl started")

    yield  # <- приложение работает здесь

    # Shutdown
    if _checker_task:
        _checker_task.cancel()
        try:
            await _checker_task
        except asyncio.CancelledError:
            pass

    await close_redis()

    # 5. Остановить Redis (только если мы его запустили)
    await _redis_manager.stop()
    logger.info("MockControl shut down")


app = FastAPI(
    title="MockControl",
    description="Centralized management system for mock services in load testing environments",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers (order is critical)
from app.api.metrics import router as metrics_router
from app.api.mocks import router as mocks_router
from app.api.hosts import router as hosts_router
from app.api.accounts import router as accounts_router
from app.api.settings import router as settings_router
from app.web.router import router as web_router
from app.proxy_handler import router as proxy_router

app.include_router(metrics_router)        # GET /metrics
app.include_router(mocks_router)          # /api/v1/mocks/...
app.include_router(hosts_router)          # /api/v1/hosts/...
app.include_router(accounts_router)       # /api/v1/accounts/...
app.include_router(settings_router)       # /api/v1/settings
app.include_router(web_router)            # /, /settings, /logs
app.include_router(proxy_router)          # /{mock_name}/{path} — MUST be last
