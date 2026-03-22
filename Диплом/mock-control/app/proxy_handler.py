"""
Catch-all proxy handler.

Registered LAST in main.py so it does not shadow /api/... or /metrics routes.
Matches: /{mock_name}/{path:path}
"""
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.core.redis_client import get_redis
from app.services.proxy import proxy_request

router = APIRouter(tags=["proxy"])

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


@router.api_route("/{mock_name}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(mock_name: str, path: str, request: Request, r: RedisDep) -> Response:
    return await proxy_request(r, mock_name, path, request)
