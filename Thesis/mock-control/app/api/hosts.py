import logging
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException

from app.core.redis_client import get_redis
from app.models.host import CreateHostRequest, HostRecord, HostStatus
from app.repositories.host_repo import (
    delete_host,
    get_host,
    host_exists,
    list_hosts,
    save_host,
)
from app.repositories.mock_repo import list_mocks

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/hosts", tags=["hosts"])

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


@router.get("", response_model=list[HostRecord])
async def list_all(r: RedisDep):
    return await list_hosts(r)


@router.post("", response_model=HostRecord, status_code=201)
async def create(body: CreateHostRequest, r: RedisDep):
    if await host_exists(r, body.hostname):
        raise HTTPException(status_code=409, detail=f"Host '{body.hostname}' already registered")
    host = HostRecord(**body.model_dump(), status=HostStatus.UNKNOWN)
    await save_host(r, host)
    return host


@router.get("/{hostname}", response_model=HostRecord)
async def get_one(hostname: str, r: RedisDep):
    host = await get_host(r, hostname)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")
    return host


@router.put("/{hostname}", response_model=HostRecord)
async def update(hostname: str, body: CreateHostRequest, r: RedisDep):
    host = await get_host(r, hostname)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")
    updated = HostRecord(
        **body.model_dump(),
        status=host.status,
        last_checked_at=host.last_checked_at,
    )
    await save_host(r, updated)
    return updated


@router.delete("/{hostname}", status_code=204)
async def remove(hostname: str, r: RedisDep):
    host = await get_host(r, hostname)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")
    # Prevent deletion if mocks are assigned to this host
    mocks = await list_mocks(r)
    assigned = [m.filename for m in mocks if m.hostname == hostname]
    if assigned:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete host '{hostname}': {len(assigned)} mock(s) still assigned: {assigned}",
        )
    await delete_host(r, hostname)
