"""Внедрение зависимостей FastAPI: объекты создаются при startup, отдаются через ``Depends``.

Соответствует ТЗ (lifespan): подключение к Redis, инициализация SSH Pool, реестра httpx,
CryptoService; при shutdown — закрытие клиентов прокси, пула SSH, Redis.
"""

from __future__ import annotations

import logging
from typing import Annotated

import core.host_resolver as host_resolver
from fastapi import Depends, FastAPI
from redis.asyncio import Redis

from config import Settings
from core.crypto import CryptoService
from core.redis_client import close_redis, get_redis, init_redis
from core.ssh_pool import SSHPool
from services.lifecycle import LifecycleManager
from services.proxy import ProxyClientRegistry
from services.rate_limiter import RateLimiter
from services.settings_service import SettingsService

logger = logging.getLogger(__name__)

_ssh_pool: SSHPool | None = None
_crypto_service: CryptoService | None = None
_proxy_registry: ProxyClientRegistry | None = None
_lifecycle_manager: LifecycleManager | None = None
_settings_service: SettingsService | None = None
_rate_limiter: RateLimiter | None = None


def get_redis_client() -> Redis:
    """Общий async-клиент Redis (после ``init_dependencies``)."""
    return get_redis()


def get_ssh_pool() -> SSHPool:
    """Пул SSH-соединений (один экземпляр на процесс)."""
    if _ssh_pool is None:
        raise RuntimeError("SSHPool не инициализирован: вызовите init_dependencies() при startup.")
    return _ssh_pool


def get_crypto_service() -> CryptoService:
    """Сервис шифрования паролей учётных записей (Fernet)."""
    if _crypto_service is None:
        raise RuntimeError(
            "CryptoService не инициализирован: вызовите init_dependencies() при startup."
        )
    return _crypto_service


def get_proxy_registry() -> ProxyClientRegistry:
    """Реестр ``httpx.AsyncClient`` по имени заглушки."""
    if _proxy_registry is None:
        raise RuntimeError(
            "ProxyClientRegistry не инициализирован: вызовите init_dependencies() при startup."
        )
    return _proxy_registry


def get_lifecycle_manager() -> LifecycleManager:
    """Менеджер жизненного цикла заглушек."""
    if _lifecycle_manager is None:
        raise RuntimeError(
            "LifecycleManager не инициализирован: вызовите init_dependencies() при startup."
        )
    return _lifecycle_manager


def get_settings_service() -> SettingsService:
    """CRUD хостов, учётных записей и глобальных настроек в Redis."""
    if _settings_service is None:
        raise RuntimeError(
            "SettingsService не инициализирован: вызовите init_dependencies() при startup."
        )
    return _settings_service


def get_rate_limiter() -> RateLimiter:
    """Rate Limiter (Fixed Window Counter в Redis)."""
    if _rate_limiter is None:
        raise RuntimeError(
            "RateLimiter не инициализирован: вызовите init_dependencies() при startup."
        )
    return _rate_limiter


RedisClientDep = Annotated[Redis, Depends(get_redis_client)]
SSHPoolDep = Annotated[SSHPool, Depends(get_ssh_pool)]
CryptoServiceDep = Annotated[CryptoService, Depends(get_crypto_service)]
ProxyRegistryDep = Annotated[ProxyClientRegistry, Depends(get_proxy_registry)]
LifecycleManagerDep = Annotated[LifecycleManager, Depends(get_lifecycle_manager)]
SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


async def init_dependencies(app: FastAPI) -> None:
    """Инициализирует глобальные зависимости и дублирует их в ``app.state`` для совместимости."""
    global _ssh_pool, _crypto_service, _proxy_registry
    global _lifecycle_manager, _settings_service, _rate_limiter

    settings = Settings()
    await init_redis(settings)
    redis = get_redis()

    _ssh_pool = SSHPool()
    _crypto_service = CryptoService(settings)
    _proxy_registry = ProxyClientRegistry()
    _settings_service = SettingsService(redis, _crypto_service)
    _rate_limiter = RateLimiter(redis)
    _lifecycle_manager = LifecycleManager(
        redis,
        _ssh_pool,
        _crypto_service,
        _proxy_registry,
        host_resolver,
    )

    app.state.settings = settings
    app.state.ssh_pool = _ssh_pool
    app.state.crypto = _crypto_service
    app.state.proxy_registry = _proxy_registry
    app.state.lifecycle_manager = _lifecycle_manager
    app.state.settings_service = _settings_service
    app.state.rate_limiter = _rate_limiter

    logger.info("Application dependencies initialized (Redis, SSH pool, proxy registry, services).")


async def shutdown_dependencies(app: FastAPI) -> None:
    """Закрывает httpx-клиенты, SSH pool и Redis; очищает ссылки."""
    global _ssh_pool, _crypto_service, _proxy_registry
    global _lifecycle_manager, _settings_service, _rate_limiter

    if _proxy_registry is not None:
        await _proxy_registry.close_all()
    if _ssh_pool is not None:
        await _ssh_pool.close_all()
    await close_redis()

    for name in (
        "settings",
        "ssh_pool",
        "crypto",
        "proxy_registry",
        "lifecycle_manager",
        "settings_service",
        "rate_limiter",
    ):
        if hasattr(app.state, name):
            delattr(app.state, name)

    _ssh_pool = None
    _crypto_service = None
    _proxy_registry = None
    _lifecycle_manager = None
    _settings_service = None
    _rate_limiter = None

    logger.info("Application dependencies shut down.")
