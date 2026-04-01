"""
Dependency Injection — централизованное создание зависимостей.

Все компоненты ядра создаются здесь и внедряются в FastAPI
через механизм Depends. Это обеспечивает единый жизненный цикл
объектов и упрощает тестирование (подмена через override).
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from redis.asyncio import Redis

from mockcontrol.config import settings
from mockcontrol.core.crypto import CryptoService
from mockcontrol.core.host_checker import HostChecker
from mockcontrol.core.lifecycle import LifecycleManager
from mockcontrol.core.metrics import MetricsExporter
from mockcontrol.core.proxy import ProxyEngine
from mockcontrol.core.rate_limiter import RateLimiter
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Singleton-контейнер: инициализируется при старте приложения (lifespan)
# --------------------------------------------------------------------------


class _Container:
    """Хранилище инициализированных зависимостей."""

    redis: Redis
    crypto: CryptoService
    mock_service: MockService
    host_service: HostService
    account_service: AccountService
    settings_service: SettingsService
    rate_limiter: RateLimiter
    lifecycle: LifecycleManager
    proxy: ProxyEngine
    metrics: MetricsExporter
    host_checker: HostChecker


container = _Container()


async def init_container() -> None:
    """
    Инициализировать все зависимости.

    Вызывается в lifespan FastAPI при старте приложения.
    """
    # Redis
    container.redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    await container.redis.ping()
    logger.info("Connected to Redis: %s", settings.redis_url)

    # Crypto
    container.crypto = CryptoService(settings.fernet_key_path)

    # Services
    container.mock_service = MockService(container.redis)
    container.host_service = HostService(container.redis)
    container.account_service = AccountService(container.redis)
    container.settings_service = SettingsService(container.redis)

    # Гарантировать наличие глобальных настроек в Redis
    await container.settings_service.ensure_defaults()

    # Core
    container.rate_limiter = RateLimiter(container.redis)

    settings.tmp_upload_dir.mkdir(parents=True, exist_ok=True)

    container.lifecycle = LifecycleManager(
        mock_service=container.mock_service,
        host_service=container.host_service,
        account_service=container.account_service,
        crypto=container.crypto,
        tmp_dir=settings.tmp_upload_dir,
    )

    container.proxy = ProxyEngine(
        mock_service=container.mock_service,
        settings_service=container.settings_service,
        rate_limiter=container.rate_limiter,
    )
    await container.proxy.startup()

    container.metrics = MetricsExporter(
        mock_service=container.mock_service,
        host_service=container.host_service,
        settings_service=container.settings_service,
        rate_limiter=container.rate_limiter,
    )

    container.host_checker = HostChecker(
        host_service=container.host_service,
        account_service=container.account_service,
        settings_service=container.settings_service,
        crypto=container.crypto,
    )

    # Reconciliation — верификация состояний после перезапуска
    changes = await container.lifecycle.reconcile()
    if changes:
        logger.info("Post-startup reconciliation: %s", changes)

    # Запуск фоновых задач
    await container.host_checker.start()

    logger.info("All dependencies initialized")


async def shutdown_container() -> None:
    """Освободить ресурсы при завершении приложения."""
    await container.host_checker.stop()
    await container.proxy.shutdown()
    await container.redis.aclose()
    logger.info("All dependencies shut down")


# --------------------------------------------------------------------------
# FastAPI Depends — геттеры для инъекции в роутеры
# --------------------------------------------------------------------------


def get_redis() -> Redis:
    return container.redis


def get_crypto() -> CryptoService:
    return container.crypto


def get_mock_service() -> MockService:
    return container.mock_service


def get_host_service() -> HostService:
    return container.host_service


def get_account_service() -> AccountService:
    return container.account_service


def get_settings_service() -> SettingsService:
    return container.settings_service


def get_rate_limiter() -> RateLimiter:
    return container.rate_limiter


def get_lifecycle() -> LifecycleManager:
    return container.lifecycle


def get_proxy() -> ProxyEngine:
    return container.proxy


def get_metrics() -> MetricsExporter:
    return container.metrics


def get_host_checker() -> HostChecker:
    return container.host_checker
