"""
При старте приложения автоматически регистрирует хост 'localhost' в реестре,
если он ещё не зарегистрирован.

Localhost-хост не требует учётной записи SSH — поле account_uuid остаётся
пустым, а lifecycle.py определяет локальный режим по hostname и использует
subprocess вместо SSH.
"""
import logging
from pathlib import Path

import redis.asyncio as aioredis

from app.models.host import HostRecord, HostStatus
from app.repositories.host_repo import get_host, save_host

logger = logging.getLogger(__name__)

LOCALHOST_HOSTNAME = "localhost"
LOCALHOST_WORKING_DIR = "./data/mocks"
LOCALHOST_JAVA_PATH = "java"          # берётся из PATH; можно переопределить в настройках


async def ensure_localhost_host(r: aioredis.Redis) -> None:
    """Создать запись localhost-хоста если её ещё нет в Redis."""
    existing = await get_host(r, LOCALHOST_HOSTNAME)
    if existing is not None:
        return

    # Создаём рабочую директорию сразу
    Path(LOCALHOST_WORKING_DIR).mkdir(parents=True, exist_ok=True)

    host = HostRecord(
        hostname=LOCALHOST_HOSTNAME,
        ssh_port=22,                  # не используется для localhost
        account_uuid="",              # не нужна учётная запись
        working_dir=LOCALHOST_WORKING_DIR,
        java_path=LOCALHOST_JAVA_PATH,
        mock_port_min=8100,
        mock_port_max=8200,
        description="Локальный хост (без SSH)",
        status=HostStatus.AVAILABLE,
    )
    await save_host(r, host)
    logger.info(
        "Registered default localhost host (working_dir=%s, java=%s)",
        LOCALHOST_WORKING_DIR,
        LOCALHOST_JAVA_PATH,
    )
