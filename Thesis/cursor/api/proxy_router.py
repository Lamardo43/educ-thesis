"""Catch-all прокси к заглушкам.

Отключено: rate limiter, запись метрик (record_proxy_observation), incr счётчиков.
Возвращено: pipeline для mock+runtime hmget, тайминги, все остальные проверки.
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
from models.mock import MockStatus
from services.proxy import ProxyClientRegistry, proxy_request

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


def _parse_mock_record(raw: list) -> tuple[str, int, MockStatus, float]:
    """hostname, port, status, timeout_sec из результата pipeline."""
    hostname_raw, port_raw, status_raw, timeout_raw = raw

    status_str = status_raw or ""
    try:
        status = MockStatus(status_str)
    except ValueError:
        status = MockStatus.STOPPED

    hostname = (hostname_raw or "").strip()

    try:
        port = int(port_raw) if port_raw not in (None, "") else 0
    except ValueError:
        port = 0

    try:
        timeout_sec = float(timeout_raw) if timeout_raw not in (None, "") else 0.0
    except ValueError:
        timeout_sec = 0.0

    return hostname, port, status, timeout_sec


async def _handle_proxy(
    mock_name: str,
    path: str,
    request: Request,
    redis: Redis,
    registry: ProxyClientRegistry,
    settings: Settings,
) -> Response:
    started_at = time.perf_counter()
    timeline: dict[str, float] = {}
    outcome = "ok"
    upstream_status: int | None = None

    def _mark(stage: str, stage_started_at: float) -> None:
        timeline[stage] = round((time.perf_counter() - stage_started_at) * 1000.0, 3)

    try:
        # Один pipeline: mock hash + settings:global за один round-trip
        t = time.perf_counter()
        pipe = redis.pipeline(transaction=False)
        pipe.hmget(
            f"mocks:{mock_name}",
            "hostname", "port", "status",
        )
        pipe.hget("settings:global", "proxy_timeout_sec")
        (mock_fields, timeout_raw) = await pipe.execute()
        _mark("redis_pipeline_ms", t)

        hostname_raw, port_raw, status_raw = mock_fields

        if status_raw is None:
            outcome = "not_found"
            raise HTTPException(status_code=404, detail="Заглушка не найдена")

        t = time.perf_counter()
        hostname, port, status, timeout_sec = _parse_mock_record(
            [hostname_raw, port_raw, status_raw, timeout_raw]
        )
        _mark("parse_mock_record_ms", t)

        if status != MockStatus.RUNNING:
            outcome = "not_running"
            raise HTTPException(status_code=503, detail="Заглушка не запущена")

        if not hostname or port <= 0:
            outcome = "invalid_target"
            raise HTTPException(status_code=503, detail="Сервис заглушки недоступен")

        if timeout_sec < 1:
            timeout_sec = float(settings.default_proxy_timeout)

        # Rate limiter — отключён
        # if rate_limit_enabled and rate_limit > 0:
        #     allowed = await rate_limiter.check(mock_name, rate_limit, window_size)
        #     if not allowed:
        #         raise HTTPException(status_code=429, detail="Превышен лимит запросов")

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
        # Запись метрик в Redis — отключена (основной источник деградации под нагрузкой)
        # try:
        #     await record_proxy_observation(redis, mock_name, timeline, outcome, upstream_status)
        # except Exception:
        #     logger.exception("Failed to persist proxy metrics for %s", mock_name)


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
) -> Response:
    """Проксирование на корень заглушки (`path` пустой)."""
    return await _handle_proxy(mock_name, "", request, redis, registry, settings)


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
) -> Response:
    """Проксирование с непустым path под заглушкой."""
    return await _handle_proxy(mock_name, path, request, redis, registry, settings)