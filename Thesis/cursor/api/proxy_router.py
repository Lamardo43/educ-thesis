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
from services.metrics import record_proxy_observation

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


def _parse_runtime_settings(
    window_raw: str | None,
    timeout_raw: str | None,
    settings: Settings,
) -> tuple[int, float]:
    """Окно rate limit и таймаут прокси из сырых значений Redis с fallback на Settings."""
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
    started_at = time.perf_counter()
    timeline: dict[str, float] = {}
    outcome = "ok"
    upstream_status: int | None = None

    def _mark(stage: str, stage_started_at: float) -> None:
        timeline[stage] = round((time.perf_counter() - stage_started_at) * 1000.0, 3)

    try:
        # Один pipeline вместо трёх последовательных round-trip к Redis:
        #   hmget mocks:{mock_name}   → данные заглушки
        #   hmget settings:global     → runtime-настройки (таймаут, окно rate limit)
        #   incr  metrics:proxy_total → счётчик запросов
        t = time.perf_counter()
        pipe = redis.pipeline(transaction=False)
        pipe.hmget(
            f"mocks:{mock_name}",
            "hostname", "port", "status", "rate_limit", "rate_limit_enabled",
        )
        pipe.hmget(
            "settings:global",
            "rate_limit_window_size", "proxy_timeout_sec",
        )
        pipe.incr(f"metrics:proxy_total:{mock_name}")
        (
            (hostname_raw, port_raw, status_raw, rate_limit_raw, rate_on_raw),
            (window_raw, timeout_raw),
            _,
        ) = await pipe.execute()
        _mark("redis_pipeline_ms", t)

        if status_raw is None:
            outcome = "not_found"
            raise HTTPException(status_code=404, detail="Заглушка не найдена")

        data = {
            "hostname": hostname_raw or "",
            "port": port_raw or "",
            "status": status_raw or "",
            "rate_limit": rate_limit_raw or "0",
            "rate_limit_enabled": rate_on_raw or "false",
        }
        t = time.perf_counter()
        hostname, port, status, rate_limit, rate_limit_enabled = _parse_mock_record(data)
        _mark("parse_mock_record_ms", t)

        if status != MockStatus.RUNNING:
            outcome = "not_running"
            raise HTTPException(status_code=503, detail="Заглушка не запущена")

        if not hostname or port <= 0:
            outcome = "invalid_target"
            raise HTTPException(status_code=503, detail="Сервис заглушки недоступен")

        window_size, timeout_sec = _parse_runtime_settings(window_raw, timeout_raw, settings)

        if rate_limit_enabled and rate_limit > 0:
            t = time.perf_counter()
            allowed = await rate_limiter.check(mock_name, rate_limit, window_size)
            _mark("rate_limiter_check_ms", t)
            if not allowed:
                t = time.perf_counter()
                await redis.incr(f"metrics:rejected_total:{mock_name}")
                _mark("redis_incr_rejected_ms", t)
                outcome = "rate_limited"
                upstream_status = 429
                raise HTTPException(status_code=429, detail="Превышен лимит запросов")

        base_url = f"http://{hostname}:{port}"
        t = time.perf_counter()
        client = await registry.get_or_create(mock_name, base_url, timeout_sec)
        _mark("httpx_client_get_or_create_ms", t)

        body: bytes | None
        t = time.perf_counter()
        if request.method in ("GET", "HEAD", "OPTIONS"):
            body = None
        else:
            body = await request.body()
        _mark("request_body_read_ms", t)

        t = time.perf_counter()
        status_code, resp_headers, resp_body, upstream_observation = await proxy_request(
            client,
            request.method,
            path,
            request.headers,
            body,
            request.url.query,
        )
        _mark("httpx_proxy_request_ms", t)
        timeline.update(upstream_observation.timings_ms)
        outcome = upstream_observation.outcome
        upstream_status = upstream_observation.upstream_status

        t = time.perf_counter()
        response = Response(
            content=resp_body,
            status_code=status_code,
            headers=resp_headers,
        )
        _mark("response_build_ms", t)
        return response
    finally:
        timeline["proxy_total_ms"] = round((time.perf_counter() - started_at) * 1000.0, 3)
        try:
            await record_proxy_observation(redis, mock_name, timeline, outcome, upstream_status)
        except Exception:
            logger.exception("Failed to persist proxy metrics for %s", mock_name)


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