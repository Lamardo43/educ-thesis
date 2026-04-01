from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends

from app.core.redis_client import get_redis
from app.models.settings import GlobalSettings
from app.repositories.settings_repo import get_settings, save_settings

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


@router.get("", response_model=GlobalSettings)
async def get(r: RedisDep):
    return await get_settings(r)


@router.put("", response_model=GlobalSettings)
async def update(body: GlobalSettings, r: RedisDep):
    await save_settings(r, body)
    return body
