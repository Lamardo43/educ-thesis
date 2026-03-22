"""
StartupReconciler — on application startup, verifies that every mock marked
RUNNING actually has a live process on the target host.

For each RUNNING mock:
  - SSH to the host and run `kill -0 {pid}`
  - If process is gone → set status=STOPPED, clear pid/port/started_at
  - If SSH fails → set status=ERROR
"""
import asyncio
import logging

import paramiko
import redis.asyncio as aioredis

from app.core.crypto import decrypt_password
from app.models.mock import MockStatus
from app.repositories.account_repo import get_account
from app.repositories.host_repo import get_host
from app.repositories.mock_repo import list_mocks, save_mock

logger = logging.getLogger(__name__)


async def reconcile(r: aioredis.Redis) -> None:
    mocks = await list_mocks(r)
    running = [m for m in mocks if m.status == MockStatus.RUNNING]
    if not running:
        logger.info("Reconciler: no RUNNING mocks to verify")
        return

    logger.info("Reconciler: verifying %d RUNNING mock(s)", len(running))

    async def _verify(mock) -> None:
        host = await get_host(r, mock.hostname)
        if host is None:
            mock.status = MockStatus.ERROR
            await save_mock(r, mock)
            return
        account = await get_account(r, host.account_uuid)
        if account is None:
            mock.status = MockStatus.ERROR
            await save_mock(r, mock)
            return

        def _check() -> bool:
            password = decrypt_password(account.password_enc)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=host.hostname,
                    port=host.ssh_port,
                    username=account.username,
                    password=password,
                    timeout=5,
                    allow_agent=False,
                    look_for_keys=False,
                )
                _, stdout, _ = client.exec_command(f"kill -0 {mock.pid} 2>/dev/null; echo $?")
                rc = stdout.read().decode().strip()
                return rc == "0"
            except Exception:
                return False
            finally:
                client.close()

        try:
            alive = await asyncio.to_thread(_check)
            if not alive:
                logger.info("Reconciler: mock '%s' PID %d not alive → STOPPED", mock.filename, mock.pid)
                mock.status = MockStatus.STOPPED
                mock.pid = None
                mock.port = None
                mock.started_at = None
                await save_mock(r, mock)
            else:
                logger.info("Reconciler: mock '%s' PID %d confirmed alive", mock.filename, mock.pid)
        except Exception as exc:
            logger.error("Reconciler: error verifying '%s': %s", mock.filename, exc)
            mock.status = MockStatus.ERROR
            await save_mock(r, mock)

    await asyncio.gather(*[_verify(m) for m in running], return_exceptions=True)
    logger.info("Reconciler: done")
