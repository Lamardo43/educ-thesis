"""
Background task: periodically checks SSH availability of all registered hosts
and updates host.status + host.last_checked_at in Redis.
"""
import asyncio
import logging
from datetime import datetime, timezone

import paramiko
import redis.asyncio as aioredis

from app.core.crypto import decrypt_password
from app.models.host import HostStatus
from app.repositories.account_repo import get_account
from app.repositories.host_repo import list_hosts, save_host
from app.repositories.settings_repo import get_settings

logger = logging.getLogger(__name__)


def _check_ssh(hostname: str, port: int, username: str, password: str) -> bool:
    """Blocking SSH connectivity check. Returns True if connection succeeds."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=hostname,
            port=port,
            username=username,
            password=password,
            timeout=5,
            allow_agent=False,
            look_for_keys=False,
        )
        return True
    except Exception:
        return False
    finally:
        client.close()


async def _check_host(r: aioredis.Redis, hostname: str) -> None:
    from app.repositories.host_repo import get_host
    from app.services.lifecycle import _is_localhost
    host = await get_host(r, hostname)
    if host is None:
        return

    # Localhost не требует SSH — всегда доступен
    if _is_localhost(hostname):
        host.status = HostStatus.AVAILABLE
        from datetime import datetime, timezone
        host.last_checked_at = datetime.now(timezone.utc)
        await save_host(r, host)
        return

    account = await get_account(r, host.account_uuid)
    if account is None:
        return
    try:
        password = decrypt_password(account.password_enc)
        alive = await asyncio.to_thread(
            _check_ssh, host.hostname, host.ssh_port, account.username, password
        )
        host.status = HostStatus.AVAILABLE if alive else HostStatus.UNAVAILABLE
    except Exception as exc:
        logger.warning("Error checking host %s: %s", hostname, exc)
        host.status = HostStatus.UNAVAILABLE
    host.last_checked_at = datetime.now(timezone.utc)
    await save_host(r, host)


async def host_checker_loop(r: aioredis.Redis) -> None:
    """Infinite loop — runs as a background asyncio task."""
    logger.info("Host checker started")
    while True:
        try:
            settings = await get_settings(r)
            hosts = await list_hosts(r)
            tasks = [_check_host(r, h.hostname) for h in hosts]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.error("Host checker error: %s", exc)
        settings = await get_settings(r)
        await asyncio.sleep(settings.host_check_interval_sec)
