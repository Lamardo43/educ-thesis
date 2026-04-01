import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.models.mock import MockRecord, MockStatus

REGISTRY_KEY = "mocks:registry"


def _key(filename: str) -> str:
    return f"mocks:{filename}"


async def get_mock(r: aioredis.Redis, filename: str) -> MockRecord | None:
    data = await r.get(_key(filename))
    if data is None:
        return None
    return MockRecord(**json.loads(data), filename=filename)


async def save_mock(r: aioredis.Redis, mock: MockRecord) -> None:
    payload = mock.model_dump(mode="json")
    payload.pop("filename")          # filename lives in the key, not the value
    await r.set(_key(mock.filename), json.dumps(payload, default=str))
    await r.sadd(REGISTRY_KEY, mock.filename)


async def delete_mock(r: aioredis.Redis, filename: str) -> None:
    await r.delete(_key(filename))
    await r.srem(REGISTRY_KEY, filename)
    # clean up any rate-limit counter keys
    async for key in r.scan_iter(f"rate:{filename}:*"):
        await r.delete(key)


async def list_mocks(r: aioredis.Redis) -> list[MockRecord]:
    filenames = await r.smembers(REGISTRY_KEY)
    if not filenames:
        return []
    pipe = r.pipeline()
    for fn in filenames:
        pipe.get(_key(fn))
    results = await pipe.execute()
    mocks = []
    for fn, data in zip(filenames, results):
        if data:
            mocks.append(MockRecord(**json.loads(data), filename=fn))
    return sorted(mocks, key=lambda m: m.registered_at)


async def mock_exists(r: aioredis.Redis, filename: str) -> bool:
    return await r.sismember(REGISTRY_KEY, filename)
