"""
Unit tests for LifecycleManager.
paramiko is fully mocked — no real SSH connections are made.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.account import AccountRecord
from app.models.host import HostRecord, HostStatus
from app.models.mock import MockRecord, MockStatus


def _mock_record(status=MockStatus.REGISTERED, pid=None, port=None):
    return MockRecord(
        filename="test-mock.jar",
        hostname="test-host",
        status=status,
        pid=pid,
        port=port,
        jvm_args="-Xmx256m",
        rate_limit=500,
        rate_limit_enabled=False,
        registered_at=datetime.now(timezone.utc),
    )


def _host_record():
    return HostRecord(
        hostname="test-host",
        ssh_port=22,
        account_uuid="acc-uuid-1",
        working_dir="/opt/mocks",
        java_path="/usr/bin/java",
        mock_port_min=8100,
        mock_port_max=8200,
        status=HostStatus.AVAILABLE,
    )


def _account_record():
    from app.core.crypto import encrypt_password
    return AccountRecord(
        uuid="acc-uuid-1",
        username="runner",
        password_enc=encrypt_password("secret"),
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_start_mock_sets_running_status():
    mock = _mock_record()
    host = _host_record()
    account = _account_record()

    r = AsyncMock()

    with (
        patch("app.services.lifecycle.get_mock", AsyncMock(return_value=mock)),
        patch("app.services.lifecycle.get_host", AsyncMock(return_value=host)),
        patch("app.services.lifecycle.get_account", AsyncMock(return_value=account)),
        patch("app.services.lifecycle.save_mock", AsyncMock()) as save_mock_fn,
        patch("app.services.lifecycle._build_ssh_client") as build_ssh,
        patch("app.services.lifecycle.asyncio.to_thread") as to_thread,
    ):
        # Simulate to_thread returning (pid, port) from the blocking _do_start closure
        to_thread.return_value = (12345, 8101)

        from app.services.lifecycle import start_mock
        result = await start_mock(r, "test-mock.jar")

    assert result.status == MockStatus.RUNNING
    assert result.pid == 12345
    assert result.port == 8101
    save_mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_stop_mock_clears_pid_and_port():
    mock = _mock_record(status=MockStatus.RUNNING, pid=12345, port=8101)
    host = _host_record()
    account = _account_record()

    r = AsyncMock()

    with (
        patch("app.services.lifecycle.get_mock", AsyncMock(return_value=mock)),
        patch("app.services.lifecycle.get_host", AsyncMock(return_value=host)),
        patch("app.services.lifecycle.get_account", AsyncMock(return_value=account)),
        patch("app.services.lifecycle.save_mock", AsyncMock()) as save_mock_fn,
        patch("app.services.lifecycle.asyncio.to_thread", AsyncMock(return_value=None)),
    ):
        from app.services.lifecycle import stop_mock
        result = await stop_mock(r, "test-mock.jar")

    assert result.status == MockStatus.STOPPED
    assert result.pid is None
    assert result.port is None
    assert result.started_at is None
    save_mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_stop_mock_already_stopped_is_noop():
    mock = _mock_record(status=MockStatus.STOPPED)
    r = AsyncMock()

    with (
        patch("app.services.lifecycle.get_mock", AsyncMock(return_value=mock)),
        patch("app.services.lifecycle.save_mock", AsyncMock()) as save_mock_fn,
    ):
        from app.services.lifecycle import stop_mock
        result = await stop_mock(r, "test-mock.jar")

    assert result.status == MockStatus.STOPPED
    # save_mock still called to normalize the record
    save_mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_start_mock_not_found_raises():
    r = AsyncMock()
    with patch("app.services.lifecycle.get_mock", AsyncMock(return_value=None)):
        from app.services.lifecycle import start_mock
        with pytest.raises(ValueError, match="not found"):
            await start_mock(r, "ghost.jar")


@pytest.mark.asyncio
async def test_register_duplicate_raises():
    r = AsyncMock()
    with patch("app.repositories.mock_repo.mock_exists", AsyncMock(return_value=True)):
        from app.services.lifecycle import register_mock
        with pytest.raises(ValueError, match="already registered"):
            await register_mock(r, "dup.jar", b"data", "host", "", "--server.port={port}", 500, False)
