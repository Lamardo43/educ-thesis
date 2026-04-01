"""
Proxy Engine — forwards incoming HTTP requests to the target mock service.

URL scheme:  /{mock_name}/{path:path}  →  http://{host}:{port}/{path}

Steps:
  1. Look up mock record in Redis.
  2. Check status == RUNNING, else 503.
  3. Apply Rate Limiter if enabled.
  4. Forward request (method, headers, body) via httpx.
  5. Stream response back unchanged.
"""
import logging

import httpx
import redis.asyncio as aioredis
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.models.mock import MockStatus
from app.repositories.mock_repo import get_mock
from app.repositories.settings_repo import get_settings
from app.services.rate_limiter import is_allowed

logger = logging.getLogger(__name__)

# Headers that must not be forwarded to the upstream service
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


async def proxy_request(r: aioredis.Redis, mock_name: str, path: str, request: Request) -> Response:
    mock = await get_mock(r, mock_name)

    if mock is None:
        return JSONResponse(status_code=404, content={"detail": f"Mock '{mock_name}' not found"})

    if mock.status != MockStatus.RUNNING or mock.port is None:
        return JSONResponse(status_code=503, content={"detail": f"Mock '{mock_name}' is not running"})

    # Rate limiting
    if mock.rate_limit_enabled:
        settings = await get_settings(r)
        allowed = await is_allowed(r, mock_name, mock.rate_limit, settings.rate_limit_window_size)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "1"},
            )

    # Build target URL
    query = request.url.query
    target_url = f"http://{mock.hostname}:{mock.port}/{path}"
    if query:
        target_url += f"?{query}"

    # Strip hop-by-hop headers
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    body = await request.body()

    settings_obj = await get_settings(r)
    timeout = settings_obj.proxy_timeout_sec

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream_response = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
            )
    except httpx.TimeoutException:
        logger.warning("Proxy timeout forwarding to mock '%s'", mock_name)
        return JSONResponse(status_code=504, content={"detail": "Upstream mock timed out"})
    except httpx.RequestError as exc:
        logger.error("Proxy error forwarding to mock '%s': %s", mock_name, exc)
        return JSONResponse(status_code=502, content={"detail": "Failed to reach mock service"})

    # Strip hop-by-hop from upstream response headers
    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
