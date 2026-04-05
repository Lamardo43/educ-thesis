"""REST API журналов заглушек: /api/v1/logs (ТЗ, раздел «REST API» → «Логи»)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis

from core.redis_client import get_redis

router = APIRouter(tags=["logs"])


def _logs_key(filename: str) -> str:
    return f"logs:{filename}"


@router.get("/{filename}", response_model=list[str])
async def get_log_lines(
    filename: str,
    redis: Annotated[Redis, Depends(get_redis)],
    lines: int = Query(1000, ge=1, le=100_000, description="Число последних строк (LRANGE -N -1)"),
) -> list[str]:
    """Последние ``lines`` строк из списка ``logs:{filename}`` (LRANGE -lines -1)."""
    return await redis.lrange(_logs_key(filename), -lines, -1)


@router.delete("/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_logs(
    filename: str,
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """Очистить буфер логов: DEL logs:{filename}."""
    await redis.delete(_logs_key(filename))
