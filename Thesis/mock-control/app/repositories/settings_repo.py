import json

import redis.asyncio as aioredis

from app.models.settings import GlobalSettings

SETTINGS_KEY = "settings:global"


async def get_settings(r: aioredis.Redis) -> GlobalSettings:
    data = await r.get(SETTINGS_KEY)
    if data is None:
        return GlobalSettings()
    return GlobalSettings(**json.loads(data))


async def save_settings(r: aioredis.Redis, s: GlobalSettings) -> None:
    await r.set(SETTINGS_KEY, s.model_dump_json())
