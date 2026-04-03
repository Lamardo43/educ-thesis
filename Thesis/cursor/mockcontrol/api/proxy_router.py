"""Catch-all прокси к заглушкам: ``/{mock_name}`` и ``/{mock_name}/{path:path}``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.responses import Response

from mockcontrol.config import settings as app_settings
from mockcontrol.core.redis_client import get_redis
from mockcontrol.services.proxy import ProxyClientRegistry, proxy_request
from mockcontrol.services.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from redis.asyncio import Redis

_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

router = APIRouter()


def get_proxy_registry(request: Request) -> ProxyClientRegistry:
    reg = getattr(request.app.state, "proxy_registry", None)
    if reg is None:
        raise RuntimeError(
            "app.state.proxy_registry не задан; укажите ProxyClientRegistry при создании приложения"
        )
    return reg


async def get_rate_limiter(redis: Redis = Depends(get_redis)) -> RateLimiter:
    return RateLimiter(redis)


async def _proxy_settings(redis: Redis) -> tuple[int, float]:
    pipe = redis.pipeline(transaction=False)
    pipe.hget("settings:global", "rate_limit_window_size")
    pipe.hget("settings:global", "proxy_timeout_sec")
    raw_window, raw_timeout = await pipe.execute()
    window = int(raw_window) if raw_window is not None else app_settings.default_rate_limit_window
    timeout_sec = int(raw_timeout) if raw_timeout is not None else app_settings.default_proxy_timeout
    return window, float(timeout_sec)


async def _handle_proxy(
    mock_name: str,
    path: str,
    request: Request,
    redis: Redis,
    rate_limiter: RateLimiter,
    registry: ProxyClientRegistry,
) -> Response:
    key = f"mocks:{mock_name}"
    record = await redis.hgetall(key)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock not found")

    status_raw = record.get("status", "")
    if status_raw != "RUNNING":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Mock is not running")

    rate_enabled = (record.get("rate_limit_enabled") or "").lower() == "true"
    try:
        rate_limit_val = int(record.get("rate_limit") or "0")
    except ValueError:
        rate_limit_val = 0

    window_size, timeout_sec = await _proxy_settings(redis)

    if rate_enabled and rate_limit_val > 0:
        allowed = await rate_limiter.check(mock_name, rate_limit_val, window_size)
        if not allowed:
            await redis.incr(f"metrics:rejected_total:{mock_name}")
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    await redis.incr(f"metrics:proxy_total:{mock_name}")

    hostname = record.get("hostname") or ""
    try:
        port = int(record.get("port") or "0")
    except ValueError:
        port = 0
    if not hostname or port <= 0:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Mock endpoint not available")
    base_url = f"http://{hostname}:{port}"
    client = await registry.get_or_create(mock_name, base_url, timeout_sec)

    body = await request.body()
    headers = dict(request.headers)
    query_string = request.url.query or None

    status_code, resp_headers, resp_body = await proxy_request(
        client,
        request.method,
        path,
        headers,
        body if body else None,
        query_string,
    )

    return Response(
        content=resp_body,
        status_code=status_code,
        headers=resp_headers,
    )


async def proxy_handler(
    mock_name: str,
    request: Request,
    redis: Redis = Depends(get_redis),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    registry: ProxyClientRegistry = Depends(get_proxy_registry),
    path: str = "",
) -> Response:
    return await _handle_proxy(mock_name, path, request, redis, rate_limiter, registry)


router.add_api_route(
    "/{mock_name}",
    proxy_handler,
    methods=list(_METHODS),
    name="proxy_mock_root",
)
router.add_api_route(
    "/{mock_name}/{path:path}",
    proxy_handler,
    methods=list(_METHODS),
    name="proxy_mock_path",
)
