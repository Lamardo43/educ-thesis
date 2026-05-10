"""Точка входа MockControl: FastAPI, lifespan, порядок маршрутов (ТЗ «Запуск приложения», «Порядок маршрутов»)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from config import Settings

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
_SINGLETON_LOCK_TTL_SEC = 30
_SINGLETON_LOCK_RENEW_INTERVAL_SEC = 10.0
_SINGLETON_LOCK_ACQUIRE_RETRY_SEC = 3.0


def _default_uvicorn_workers() -> int:
    """Авточисло воркеров: CPU×2, минимум 1 (если cpu_count недоступен — считаем 1 CPU)."""
    cpus = os.cpu_count() or 1
    return max(1, 2 * cpus)
    # return 1


def resolve_uvicorn_workers(settings: Settings) -> int:
    """Явное значение из Settings (UVICORN_WORKERS / uvicorn_workers) или расчёт по CPU."""
    if settings.uvicorn_workers is not None:
        return settings.uvicorn_workers
    return _default_uvicorn_workers()


def _configure_quiet_logging() -> None:
    """INFO для приложения и uvicorn.error; без access-log (каждый GET), без httpx/httpcore и без спама asyncssh."""
    # logging.getLogger().setLevel(logging.INFO)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for name in ("uvicorn.access", "httpx", "httpcore", "asyncssh"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _singleton_lock_key(task_name: str) -> str:
    return f"singleton:task:{task_name}"


async def _acquire_singleton_lock(redis: Redis, lock_key: str, owner_id: str) -> bool:
    got = await redis.set(lock_key, owner_id, ex=_SINGLETON_LOCK_TTL_SEC, nx=True)
    return bool(got)


async def _renew_singleton_lock(redis: Redis, lock_key: str, owner_id: str) -> bool:
    renewed = await redis.eval(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """,
        1,
        lock_key,
        owner_id,
        str(_SINGLETON_LOCK_TTL_SEC),
    )
    return bool(int(renewed))


async def _release_singleton_lock(redis: Redis, lock_key: str, owner_id: str) -> None:
    await redis.eval(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """,
        1,
        lock_key,
        owner_id,
    )


async def _run_singleton_background_task(
    task_name: str,
    redis: Redis,
    task_factory: Callable[[], Awaitable[None]],
) -> None:
    """Запускает фоновый task только у одного worker (через Redis lock + heartbeat)."""
    lock_key = _singleton_lock_key(task_name)
    owner_id = f"{os.getpid()}:{uuid.uuid4().hex}"
    worker_task: asyncio.Task[None] | None = None
    try:
        while True:
            try:
                got_lock = await _acquire_singleton_lock(redis, lock_key, owner_id)
            except Exception:
                logger.exception("Singleton %s: failed to acquire lock, retrying", task_name)
                await asyncio.sleep(_SINGLETON_LOCK_ACQUIRE_RETRY_SEC)
                continue

            if not got_lock:
                await asyncio.sleep(_SINGLETON_LOCK_ACQUIRE_RETRY_SEC)
                continue

            logger.info("Singleton %s: lock acquired by pid=%s", task_name, os.getpid())
            worker_task = asyncio.create_task(task_factory(), name=task_name)
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {worker_task},
                        timeout=_SINGLETON_LOCK_RENEW_INTERVAL_SEC,
                    )
                    if done:
                        try:
                            worker_task.result()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("Singleton %s: background task crashed", task_name)
                        else:
                            logger.warning("Singleton %s: background task finished unexpectedly", task_name)
                        worker_task = None
                        break

                    try:
                        renewed = await _renew_singleton_lock(redis, lock_key, owner_id)
                    except Exception:
                        logger.exception("Singleton %s: lock heartbeat failed", task_name)
                        renewed = False
                    if renewed:
                        continue
                    logger.warning("Singleton %s: lock lost, cancelling local task", task_name)
                    worker_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker_task
                    worker_task = None
                    break
            finally:
                with contextlib.suppress(Exception):
                    await _release_singleton_lock(redis, lock_key, owner_id)

            await asyncio.sleep(_SINGLETON_LOCK_ACQUIRE_RETRY_SEC)
    except asyncio.CancelledError:
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        with contextlib.suppress(Exception):
            await _release_singleton_lock(redis, lock_key, owner_id)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: зависимости, глобальные настройки Redis, восстановление RUNNING, фоновые задачи. Shutdown: отмена задач, закрытие ресурсов."""
    # logging.basicConfig(
    #     level=logging.INFO,
    #     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    # )
    _configure_quiet_logging()
    await init_dependencies(app)

    global_settings = await app.state.settings_service.get_settings()
    await app.state.lifecycle_manager.restore_state()

    redis = get_redis()
    ssh_pool = app.state.ssh_pool
    crypto = app.state.crypto
    settings = app.state.settings

    if global_settings.enable_health_checker:
        app.state.health_checker_task = asyncio.create_task(
            _run_singleton_background_task(
                "health_checker",
                redis,
                lambda: run_health_checker(redis, ssh_pool, crypto, settings),
            ),
            name="health_checker_singleton_supervisor",
        )
    else:
        logger.warning("Health checker is disabled via settings:global.enable_health_checker")

    if global_settings.enable_log_collector:
        app.state.log_collector_task = asyncio.create_task(
            _run_singleton_background_task(
                "log_collector",
                redis,
                lambda: run_log_collector(redis, ssh_pool, crypto, settings),
            ),
            name="log_collector_singleton_supervisor",
        )
    else:
        logger.warning("Log collector is disabled via settings:global.enable_log_collector")

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
    # logging.basicConfig(
    #     level=logging.INFO,
    #     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    # )
    _configure_quiet_logging()
    _settings = Settings()
    workers = resolve_uvicorn_workers(_settings)
    logger.info("Uvicorn workers: %s (cpus=%s)", workers, os.cpu_count())
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=False,
        workers=workers,
    )
