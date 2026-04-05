"""Catch-all прокси к заглушкам (ТЗ: Proxy Engine, критические нюансы).

Подключается в FastAPI последним: маршруты `/{mock_name}` и `/{mock_name}/{path:path}`.
Требуется `app.state.proxy_registry: ProxyClientRegistry`, задаётся при старте приложения.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis
from starlette.responses import Response

from config import Settings
from core.redis_client import get_redis
from dependencies import get_rate_limiter
from models.mock import MockStatus
from services.proxy import ProxyClientRegistry, proxy_request
from services.rate_limiter import RateLimiter

_PROXY_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

router = APIRouter(tags=["proxy"])
logger = logging.getLogger(__name__)


@lru_cache
def get_settings() -> Settings:
    """Настройки приложения (кэш на процесс)."""
    return Settings()


def get_proxy_registry(request: Request) -> ProxyClientRegistry:
    """Реестр httpx-клиентов из состояния приложения."""
    reg = getattr(request.app.state, "proxy_registry", None)
    if not isinstance(reg, ProxyClientRegistry):
        raise RuntimeError(
            "Не задан app.state.proxy_registry: ProxyClientRegistry (инициализация в main)."
        )
    return reg


async def _load_proxy_runtime(redis: Redis, settings: Settings) -> tuple[int, float]:
    """Окно rate limit и таймаут прокси из `settings:global` с fallback на Settings."""
    window_raw, timeout_raw = await redis.hmget(
        "settings:global",
        "rate_limit_window_size",
        "proxy_timeout_sec",
    )
    try:
        window = int(window_raw) if window_raw is not None and str(window_raw).strip() else settings.default_rate_limit_window
    except ValueError:
        window = settings.default_rate_limit_window
    try:
        timeout_sec = int(timeout_raw) if timeout_raw is not None and str(timeout_raw).strip() else settings.default_proxy_timeout
    except ValueError:
        timeout_sec = settings.default_proxy_timeout
    if window < 1:
        window = settings.default_rate_limit_window
    if timeout_sec < 1:
        timeout_sec = settings.default_proxy_timeout
    return window, float(timeout_sec)


def _parse_mock_record(raw: dict[str, str]) -> tuple[str, int, MockStatus, int, bool]:
    """hostname, port, status, rate_limit, rate_limit_enabled из Hash."""
    status_str = raw.get("status") or ""
    try:
        status = MockStatus(status_str)
    except ValueError:
        status = MockStatus.STOPPED
    hostname = (raw.get("hostname") or "").strip()
    try:
        port = int(raw["port"]) if raw.get("port") not in (None, "") else 0
    except ValueError:
        port = 0
    try:
        rate_limit = int(raw.get("rate_limit") or "0")
    except ValueError:
        rate_limit = 0
    rate_on = (raw.get("rate_limit_enabled") or "false").lower() == "true"
    return hostname, port, status, rate_limit, rate_on


async def _handle_proxy(
    mock_name: str,
    path: str,
    request: Request,
    redis: Redis,
    registry: ProxyClientRegistry,
    settings: Settings,
    rate_limiter: RateLimiter,
) -> Response:
    req_started = time.perf_counter()
    key = f"mocks:{mock_name}"
    t0 = time.perf_counter()
    hostname_raw, port_raw, status_raw, rate_limit_raw, rate_on_raw = await redis.hmget(
        key,
        "hostname",
        "port",
        "status",
        "rate_limit",
        "rate_limit_enabled",
    )
    mock_lookup_ms = (time.perf_counter() - t0) * 1000.0
    if status_raw is None:
        logger.info(
            "proxy_timing mock=%s step=mock_lookup status=not_found elapsed_ms=%.2f",
            mock_name,
            mock_lookup_ms,
        )
        raise HTTPException(status_code=404, detail="Заглушка не найдена")
    data = {
        "hostname": hostname_raw or "",
        "port": port_raw or "",
        "status": status_raw or "",
        "rate_limit": rate_limit_raw or "0",
        "rate_limit_enabled": rate_on_raw or "false",
    }
    hostname, port, status, rate_limit, rate_limit_enabled = _parse_mock_record(data)

    if status != MockStatus.RUNNING:
        logger.info(
            "proxy_timing mock=%s method=%s status=503_not_running mock_lookup_ms=%.2f total_ms=%.2f",
            mock_name,
            request.method,
            mock_lookup_ms,
            (time.perf_counter() - req_started) * 1000.0,
        )
        raise HTTPException(status_code=503, detail="Заглушка не запущена")

    if not hostname or port <= 0:
        logger.info(
            "proxy_timing mock=%s method=%s status=503_unavailable mock_lookup_ms=%.2f total_ms=%.2f",
            mock_name,
            request.method,
            mock_lookup_ms,
            (time.perf_counter() - req_started) * 1000.0,
        )
        raise HTTPException(status_code=503, detail="Сервис заглушки недоступен")

    t0 = time.perf_counter()
    window_size, timeout_sec = await _load_proxy_runtime(redis, settings)
    runtime_lookup_ms = (time.perf_counter() - t0) * 1000.0

    if rate_limit_enabled and rate_limit > 0:
        t0 = time.perf_counter()
        allowed = await rate_limiter.check(mock_name, rate_limit, window_size)
        rate_limit_ms = (time.perf_counter() - t0) * 1000.0
    else:
        allowed = True
        rate_limit_ms = 0.0

    if rate_limit_enabled and rate_limit > 0:
        if not allowed:
            t0 = time.perf_counter()
            await redis.incr(f"metrics:rejected_total:{mock_name}")
            rejected_metric_ms = (time.perf_counter() - t0) * 1000.0
            total_ms = (time.perf_counter() - req_started) * 1000.0
            logger.info(
                "proxy_timing mock=%s method=%s status=429 mock_lookup_ms=%.2f "
                "runtime_lookup_ms=%.2f rate_limit_ms=%.2f rejected_metric_ms=%.2f total_ms=%.2f",
                mock_name,
                request.method,
                mock_lookup_ms,
                runtime_lookup_ms,
                rate_limit_ms,
                rejected_metric_ms,
                total_ms,
            )
            raise HTTPException(status_code=429, detail="Превышен лимит запросов")

    t0 = time.perf_counter()
    await redis.incr(f"metrics:proxy_total:{mock_name}")
    proxy_metric_ms = (time.perf_counter() - t0) * 1000.0

    base_url = f"http://{hostname}:{port}"
    t0 = time.perf_counter()
    client = await registry.get_or_create(mock_name, base_url, timeout_sec)
    client_lookup_ms = (time.perf_counter() - t0) * 1000.0

    body: bytes | None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        body = None
        body_read_ms = 0.0
    else:
        t0 = time.perf_counter()
        body = await request.body()
        body_read_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    status_code, resp_headers, resp_body = await proxy_request(
        client,
        request.method,
        path,
        request.headers,
        body,
        request.url.query,
    )
    upstream_ms = (time.perf_counter() - t0) * 1000.0
    total_ms = (time.perf_counter() - req_started) * 1000.0
    logger.info(
        "proxy_timing mock=%s method=%s status=%s mock_lookup_ms=%.2f runtime_lookup_ms=%.2f "
        "rate_limit_ms=%.2f proxy_metric_ms=%.2f client_lookup_ms=%.2f body_read_ms=%.2f "
        "upstream_ms=%.2f total_ms=%.2f",
        mock_name,
        request.method,
        status_code,
        mock_lookup_ms,
        runtime_lookup_ms,
        rate_limit_ms,
        proxy_metric_ms,
        client_lookup_ms,
        body_read_ms,
        upstream_ms,
        total_ms,
    )

    return Response(
        content=resp_body,
        status_code=status_code,
        headers=resp_headers,
    )


@router.api_route(
    "/{mock_name}",
    methods=list(_PROXY_METHODS),
    name="proxy_mock_root",
)
async def proxy_to_mock_root(
    mock_name: str,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    registry: Annotated[ProxyClientRegistry, Depends(get_proxy_registry)],
    settings: Annotated[Settings, Depends(get_settings)],
    rate_limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> Response:
    """Проксирование на корень заглушки (`path` пустой)."""
    return await _handle_proxy(
        mock_name, "", request, redis, registry, settings, rate_limiter
    )


@router.api_route(
    "/{mock_name}/{path:path}",
    methods=list(_PROXY_METHODS),
    name="proxy_mock_path",
)
async def proxy_to_mock_path(
    mock_name: str,
    path: str,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    registry: Annotated[ProxyClientRegistry, Depends(get_proxy_registry)],
    settings: Annotated[Settings, Depends(get_settings)],
    rate_limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> Response:
    """Проксирование с непустым path под заглушкой."""
    return await _handle_proxy(
        mock_name, path, request, redis, registry, settings, rate_limiter
    )
