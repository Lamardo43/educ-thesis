"""
Integration tests for REST API endpoints.
Redis and SSH calls are mocked via dependency overrides and patches.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.redis_client import get_redis
from app.models.mock import MockRecord, MockStatus
from app.models.host import HostRecord, HostStatus
from app.models.settings import GlobalSettings


# ── App with overridden Redis dep ────────────────────────────────────────
def _make_app(mock_redis: AsyncMock):
    from app.main import app
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


def _sample_mock(status=MockStatus.REGISTERED):
    return MockRecord(
        filename="api-test.jar",
        hostname="localhost",
        status=status,
        pid=None,
        port=None,
        jvm_args="",
        rate_limit=500,
        rate_limit_enabled=False,
        registered_at=datetime.now(timezone.utc),
    )


def _sample_host():
    return HostRecord(
        hostname="localhost",
        ssh_port=22,
        account_uuid="test-uuid",
        working_dir="/opt/mocks",
        java_path="/usr/bin/java",
        mock_port_min=8100,
        mock_port_max=8200,
        status=HostStatus.AVAILABLE,
    )


@pytest.fixture
def redis_mock():
    r = AsyncMock()
    r.ping = AsyncMock(return_value=True)
    return r


# ── GET /api/v1/mocks ────────────────────────────────────────────────────

def test_list_mocks_empty(redis_mock):
    with patch("app.api.mocks.list_mocks", AsyncMock(return_value=[])):
        client = TestClient(_make_app(redis_mock), raise_server_exceptions=True)
        resp = client.get("/api/v1/mocks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_mocks_returns_records(redis_mock):
    mocks = [_sample_mock()]
    with patch("app.api.mocks.list_mocks", AsyncMock(return_value=mocks)):
        client = TestClient(_make_app(redis_mock))
        resp = client.get("/api/v1/mocks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["filename"] == "api-test.jar"


# ── GET /api/v1/mocks/{name} ─────────────────────────────────────────────

def test_get_mock_not_found(redis_mock):
    with patch("app.api.mocks.get_mock", AsyncMock(return_value=None)):
        client = TestClient(_make_app(redis_mock))
        resp = client.get("/api/v1/mocks/ghost.jar")
    assert resp.status_code == 404


def test_get_mock_found(redis_mock):
    with patch("app.api.mocks.get_mock", AsyncMock(return_value=_sample_mock())):
        client = TestClient(_make_app(redis_mock))
        resp = client.get("/api/v1/mocks/api-test.jar")
    assert resp.status_code == 200
    assert resp.json()["filename"] == "api-test.jar"


# ── POST /api/v1/mocks/{name}/start ─────────────────────────────────────

def test_start_mock(redis_mock):
    running = _sample_mock(MockStatus.RUNNING)
    running.pid = 555
    running.port = 8101
    with patch("app.api.mocks.start_mock", AsyncMock(return_value=running)):
        client = TestClient(_make_app(redis_mock))
        resp = client.post("/api/v1/mocks/api-test.jar/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "RUNNING"


# ── POST /api/v1/mocks/{name}/stop ──────────────────────────────────────

def test_stop_mock(redis_mock):
    stopped = _sample_mock(MockStatus.STOPPED)
    with patch("app.api.mocks.stop_mock", AsyncMock(return_value=stopped)):
        client = TestClient(_make_app(redis_mock))
        resp = client.post("/api/v1/mocks/api-test.jar/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "STOPPED"


# ── PATCH /api/v1/mocks/{name}/rate-limit ───────────────────────────────

def test_toggle_rate_limit(redis_mock):
    mock = _sample_mock()
    with (
        patch("app.api.mocks.get_mock", AsyncMock(return_value=mock)),
        patch("app.api.mocks.save_mock", AsyncMock()),
    ):
        client = TestClient(_make_app(redis_mock))
        resp = client.patch(
            "/api/v1/mocks/api-test.jar/rate-limit",
            json={"enabled": True, "rate_limit": 200},
        )
    assert resp.status_code == 200
    assert resp.json()["rate_limit_enabled"] is True
    assert resp.json()["rate_limit"] == 200


# ── DELETE /api/v1/mocks/{name} ──────────────────────────────────────────

def test_delete_mock(redis_mock):
    with (
        patch("app.api.mocks.get_mock", AsyncMock(return_value=_sample_mock())),
        patch("app.api.mocks.delete_mock_service", AsyncMock()),
    ):
        client = TestClient(_make_app(redis_mock))
        resp = client.delete("/api/v1/mocks/api-test.jar")
    assert resp.status_code == 204


# ── GET /api/v1/settings ─────────────────────────────────────────────────

def test_get_settings(redis_mock):
    with patch("app.api.settings.get_settings", AsyncMock(return_value=GlobalSettings())):
        client = TestClient(_make_app(redis_mock))
        resp = client.get("/api/v1/settings")
    assert resp.status_code == 200
    assert resp.json()["proxy_timeout_sec"] == 10


# ── GET /metrics ─────────────────────────────────────────────────────────

def test_metrics_endpoint(redis_mock):
    # pipeline() должен возвращать синхронный объект с async execute()
    mock_pipe = AsyncMock()
    mock_pipe.get = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=[])
    redis_mock.pipeline = MagicMock(return_value=mock_pipe)

    with (
        patch("app.services.metrics.list_mocks", AsyncMock(return_value=[])),
        patch("app.services.metrics.list_hosts", AsyncMock(return_value=[])),
        patch("app.services.metrics.get_settings", AsyncMock(return_value=GlobalSettings())),
    ):
        client = TestClient(_make_app(redis_mock))
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "mock_status_running" in resp.text
    assert "host_available" in resp.text
