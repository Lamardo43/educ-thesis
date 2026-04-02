"""
Pytest fixtures для тестирования MockControl.

Предоставляет:
- Мок-Redis (fakeredis) для изолированных тестов без внешних зависимостей
- Фабрики моделей данных (заглушки, хосты, учётные записи)
- Преднастроенные экземпляры сервисов и компонентов ядра
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

try:
    from fakeredis.aioredis import FakeRedis
except ImportError:
    # Fallback: тесты, требующие Redis, будут пропущены
    FakeRedis = None

from mockcontrol.core.crypto import CryptoService
from mockcontrol.core.rate_limiter import RateLimiter
from mockcontrol.models.account import AccountConfig
from mockcontrol.models.host import HostConfig, HostStatus
from mockcontrol.models.mock import MockConfig, MockStatus
from mockcontrol.models.settings import GlobalSettings
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService


# -------------------------------------------------------------------------
# Redis fixture
# -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis():
    """Изолированный экземпляр FakeRedis для каждого теста."""
    if FakeRedis is None:
        pytest.skip("fakeredis not installed")
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


# -------------------------------------------------------------------------
# Service fixtures
# -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mock_service(redis) -> MockService:
    return MockService(redis)


@pytest_asyncio.fixture
async def host_service(redis) -> HostService:
    return HostService(redis)


@pytest_asyncio.fixture
async def account_service(redis) -> AccountService:
    return AccountService(redis)


@pytest_asyncio.fixture
async def settings_service(redis) -> SettingsService:
    return SettingsService(redis)


# -------------------------------------------------------------------------
# Core fixtures
# -------------------------------------------------------------------------


@pytest.fixture
def crypto(tmp_path) -> CryptoService:
    """CryptoService с временным ключом."""
    key_path = tmp_path / "test_fernet.key"
    return CryptoService(key_path)


@pytest_asyncio.fixture
async def rate_limiter(redis) -> RateLimiter:
    return RateLimiter(redis)


# -------------------------------------------------------------------------
# Model factories
# -------------------------------------------------------------------------


@pytest.fixture
def make_mock_config():
    """Фабрика конфигураций заглушек с значениями по умолчанию."""

    def _factory(
        hostname: str = "test-server-01",
        status: MockStatus = MockStatus.REGISTERED,
        port: int | None = None,
        pid: int | None = None,
        jvm_args: str = "-Xms128m -Xmx256m",
        rate_limit: int = 500,
        rate_limit_enabled: bool = False,
    ) -> MockConfig:
        return MockConfig(
            hostname=hostname,
            port=port,
            pid=pid,
            status=status,
            jvm_args=jvm_args,
            rate_limit=rate_limit,
            rate_limit_enabled=rate_limit_enabled,
            registered_at=datetime(2025, 1, 1, 10, 0, 0),
            started_at=datetime(2025, 1, 1, 10, 5, 0) if pid else None,
        )

    return _factory


@pytest.fixture
def make_host_config():
    """Фабрика конфигураций хостов."""

    def _factory(
        ssh_port: int = 22,
        account_uuid: str = "e5f6a7b8-c9d0-1234-efab-567890abcdef",
        working_dir: str = "/opt/mock-services",
        java_path: str = "/usr/bin/java",
        mock_port_min: int = 8100,
        mock_port_max: int = 8200,
        status: HostStatus = HostStatus.AVAILABLE,
    ) -> HostConfig:
        return HostConfig(
            ssh_port=ssh_port,
            account_uuid=account_uuid,
            working_dir=working_dir,
            java_path=java_path,
            mock_port_min=mock_port_min,
            mock_port_max=mock_port_max,
            status=status,
        )

    return _factory


@pytest.fixture
def make_account_config(crypto):
    """Фабрика учётных записей с зашифрованным паролем."""

    def _factory(
        username: str = "mock-runner",
        password: str = "secret123",
        description: str = "Test account",
    ) -> AccountConfig:
        return AccountConfig(
            username=username,
            password_enc=crypto.encrypt(password),
            description=description,
        )

    return _factory
