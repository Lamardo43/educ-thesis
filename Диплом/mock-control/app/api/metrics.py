from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.core.redis_client import get_redis
from app.services.metrics import generate_metrics

router = APIRouter(tags=["metrics"])

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(r: RedisDep):
    content = await generate_metrics(r)
    return PlainTextResponse(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
