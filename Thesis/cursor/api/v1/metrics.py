"""GET /metrics — Prometheus exposition (ТЗ, раздел «Metrics Exporter»)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from starlette.responses import Response

from core.redis_client import get_redis
from services.metrics import collect_metrics

router = APIRouter(tags=["metrics"])

_METRICS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    body = await collect_metrics(redis)
    return Response(content=body, media_type=_METRICS_MEDIA_TYPE)
