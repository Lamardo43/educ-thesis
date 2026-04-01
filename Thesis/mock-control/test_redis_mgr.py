import asyncio, sys
sys.path.insert(0, '.')
from app.core.redis_server import EmbeddedRedisManager

async def test():
    mgr = EmbeddedRedisManager('127.0.0.1', 6399, '/tmp/test-redis-data')
    print('Before start:', mgr._is_redis_available())
    await mgr.start()
    print('After start:', mgr._is_redis_available(), 'PID:', mgr._process.pid)

    import redis.asyncio as aioredis
    r = aioredis.from_url('redis://127.0.0.1:6399/0', decode_responses=True)
    await r.set('k', 'v')
    val = await r.get('k')
    print('GET:', val)
    assert val == 'v'
    await r.aclose()

    await mgr.stop()
    print('After stop:', mgr._is_redis_available())
    print('ALL PASSED')

asyncio.run(test())
