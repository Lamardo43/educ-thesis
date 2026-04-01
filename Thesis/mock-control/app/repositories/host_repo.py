import json

import redis.asyncio as aioredis

from app.models.host import HostRecord

REGISTRY_KEY = "hosts:registry"


def _key(hostname: str) -> str:
    return f"hosts:{hostname}"


async def get_host(r: aioredis.Redis, hostname: str) -> HostRecord | None:
    data = await r.get(_key(hostname))
    if data is None:
        return None
    return HostRecord(**json.loads(data), hostname=hostname)


async def save_host(r: aioredis.Redis, host: HostRecord) -> None:
    payload = host.model_dump(mode="json")
    payload.pop("hostname")
    await r.set(_key(host.hostname), json.dumps(payload, default=str))
    await r.sadd(REGISTRY_KEY, host.hostname)


async def delete_host(r: aioredis.Redis, hostname: str) -> None:
    await r.delete(_key(hostname))
    await r.srem(REGISTRY_KEY, hostname)


async def list_hosts(r: aioredis.Redis) -> list[HostRecord]:
    hostnames = await r.smembers(REGISTRY_KEY)
    if not hostnames:
        return []
    pipe = r.pipeline()
    for h in hostnames:
        pipe.get(_key(h))
    results = await pipe.execute()
    hosts = []
    for h, data in zip(hostnames, results):
        if data:
            hosts.append(HostRecord(**json.loads(data), hostname=h))
    return sorted(hosts, key=lambda x: x.hostname)


async def host_exists(r: aioredis.Redis, hostname: str) -> bool:
    return await r.sismember(REGISTRY_KEY, hostname)
