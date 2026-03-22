import json

import redis.asyncio as aioredis

from app.models.account import AccountRecord

REGISTRY_KEY = "accounts:registry"


def _key(uuid: str) -> str:
    return f"accounts:{uuid}"


async def get_account(r: aioredis.Redis, uuid: str) -> AccountRecord | None:
    data = await r.get(_key(uuid))
    if data is None:
        return None
    return AccountRecord(**json.loads(data), uuid=uuid)


async def save_account(r: aioredis.Redis, account: AccountRecord) -> None:
    payload = account.model_dump(mode="json")
    payload.pop("uuid")
    await r.set(_key(account.uuid), json.dumps(payload, default=str))
    await r.sadd(REGISTRY_KEY, account.uuid)


async def delete_account(r: aioredis.Redis, uuid: str) -> None:
    await r.delete(_key(uuid))
    await r.srem(REGISTRY_KEY, uuid)


async def list_accounts(r: aioredis.Redis) -> list[AccountRecord]:
    uuids = await r.smembers(REGISTRY_KEY)
    if not uuids:
        return []
    pipe = r.pipeline()
    for u in uuids:
        pipe.get(_key(u))
    results = await pipe.execute()
    accounts = []
    for u, data in zip(uuids, results):
        if data:
            accounts.append(AccountRecord(**json.loads(data), uuid=u))
    return sorted(accounts, key=lambda a: a.created_at)


async def account_exists(r: aioredis.Redis, uuid: str) -> bool:
    return await r.sismember(REGISTRY_KEY, uuid)
