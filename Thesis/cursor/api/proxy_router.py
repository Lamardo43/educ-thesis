"""Catch-all прокси к заглушкам.

Горячий путь: один Redis pipeline (mock hash + settings:global),
rate limiter через pipeline (INCR + EXPIRE за один round-trip),
запись метрик — fire-and-forget через asyncio.create_task (клиент не ждёт).
"""

from __future__ import annotations

import asyncio
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
from services.metrics import record_proxy_observation
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


def _parse_mock_record(raw: list) -> tuple[str, int, MockStatus, int, bool, int, float]:
    """hostname, port, status, rate_limit, rate_limit_enabled, window_size, timeout_sec."""
    hostname_raw, port_raw, status_raw, rate_limit_raw, rate_on_raw, window_raw, timeout_raw = raw

    try:
        status = MockStatus(status_raw or "")
    except ValueError:
        status = MockStatus.STOPPED

    hostname = (hostname_raw or "").strip()

    try:
        port = int(port_raw) if port_raw not in (None, "") else 0
    except ValueError:
        port = 0

    try:
        rate_limit = int(rate_limit_raw) if rate_limit_raw not in (None, "") else 0
    except ValueError:
        rate_limit = 0

    rate_limit_enabled = (rate_on_raw or "false").lower() == "true"

    try:
        window_size = int(window_raw) if window_raw not in (None, "") else 0
    except ValueError:
        window_size = 0

    try:
        timeout_sec = float(timeout_raw) if timeout_raw not in (None, "") else 0.0
    except ValueError:
        timeout_sec = 0.0

    return hostname, port, status, rate_limit, rate_limit_enabled, window_size, timeout_sec


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
        # Pipeline 1: mock hash + settings:global за один round-trip
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
        mock_fields, settings_fields = await pipe.execute()
        _mark("redis_pipeline_ms", t)

        hostname_raw, port_raw, status_raw, rate_limit_raw, rate_on_raw = mock_fields
        window_raw, timeout_raw = settings_fields

        if status_raw is None:
            outcome = "not_found"
            raise HTTPException(status_code=404, detail="Заглушка не найдена")

        t = time.perf_counter()
        hostname, port, status, rate_limit, rate_limit_enabled, window_size, timeout_sec = (
            _parse_mock_record([
                hostname_raw, port_raw, status_raw,
                rate_limit_raw, rate_on_raw,
                window_raw, timeout_raw,
            ])
        )
        _mark("parse_mock_record_ms", t)

        if status != MockStatus.RUNNING:
            outcome = "not_running"
            raise HTTPException(status_code=503, detail="Заглушка не запущена")

        if not hostname or port <= 0:
            outcome = "invalid_target"
            raise HTTPException(status_code=503, detail="Сервис заглушки недоступен")

        if window_size < 1:
            window_size = settings.default_rate_limit_window
        if timeout_sec < 1:
            timeout_sec = float(settings.default_proxy_timeout)

        # Rate limiter: INCR + условный EXPIRE за один pipeline round-trip
        if rate_limit_enabled and rate_limit > 0:
            t = time.perf_counter()
            window = int(time.time()) // window_size
            rate_key = f"rate:{mock_name}:{window}"

            pipe2 = redis.pipeline(transaction=False)
            pipe2.incr(rate_key)
            pipe2.expire(rate_key, window_size * 2)
            results = await pipe2.execute()
            count = results[0]
            _mark("rate_limiter_check_ms", t)

            if count > rate_limit:
                outcome = "rate_limited"
                upstream_status = 429
                raise HTTPException(status_code=429, detail="Превышен лимит запросов")

        base_url = f"http://{hostname}:{port}"
        t = time.perf_counter()
        client = await registry.get_or_create(mock_name, base_url, timeout_sec)
        _mark("httpx_client_get_or_create_ms", t)

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
        # Fire-and-forget: клиент получает ответ не дожидаясь записи ~90 Redis команд
        asyncio.create_task(
            record_proxy_observation(redis, mock_name, timeline, outcome, upstream_status)
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
    return await _handle_proxy(mock_name, "", request, redis, registry, settings, rate_limiter)


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
    return await _handle_proxy(mock_name, path, request, redis, registry, settings, rate_limiter)