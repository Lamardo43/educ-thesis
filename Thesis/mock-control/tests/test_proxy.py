"""
Unit tests for the ProxyEngine.
httpx and Redis are mocked.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.models.mock import MockRecord, MockStatus
from app.models.settings import GlobalSettings


def _running_mock():
    return MockRecord(
        filename="svc.jar",
        hostname="127.0.0.1",
        port=8101,
        pid=999,
        status=MockStatus.RUNNING,
        jvm_args="",
        rate_limit=100,
        rate_limit_enabled=False,
        registered_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )


def _build_request(method="GET", path="/", body=b""):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_proxy_returns_503_when_mock_stopped():
    stopped = _running_mock()
    stopped.status = MockStatus.STOPPED
    stopped.port = None

    r = AsyncMock()
    request = _build_request()

    with patch("app.services.proxy.get_mock", AsyncMock(return_value=stopped)):
        from app.services.proxy import proxy_request
        resp = await proxy_request(r, "svc.jar", "api/status", request)

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_proxy_returns_404_when_mock_not_found():
    r = AsyncMock()
    request = _build_request()

    with patch("app.services.proxy.get_mock", AsyncMock(return_value=None)):
        from app.services.proxy import proxy_request
        resp = await proxy_request(r, "ghost.jar", "any/path", request)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_proxy_returns_429_when_rate_limit_exceeded():
    mock = _running_mock()
    mock.rate_limit_enabled = True

    r = AsyncMock()
    request = _build_request()

    with (
        patch("app.services.proxy.get_mock", AsyncMock(return_value=mock)),
        patch("app.services.proxy.get_settings", AsyncMock(return_value=GlobalSettings())),
        patch("app.services.proxy.is_allowed", AsyncMock(return_value=False)),
    ):
        from app.services.proxy import proxy_request
        resp = await proxy_request(r, "svc.jar", "any", request)

    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_proxy_forwards_request_and_returns_upstream_response():
    mock = _running_mock()
    r = AsyncMock()
    request = _build_request(method="GET")

    import httpx
    fake_response = httpx.Response(200, content=b'{"ok":true}',
                                   headers={"content-type": "application/json"})

    with (
        patch("app.services.proxy.get_mock", AsyncMock(return_value=mock)),
        patch("app.services.proxy.get_settings", AsyncMock(return_value=GlobalSettings())),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=fake_response)
        mock_client_cls.return_value = mock_client

        from app.services.proxy import proxy_request
        resp = await proxy_request(r, "svc.jar", "api/health", request)

    assert resp.status_code == 200
    assert resp.body == b'{"ok":true}'


@pytest.mark.asyncio
async def test_proxy_returns_504_on_timeout():
    mock = _running_mock()
    r = AsyncMock()
    request = _build_request()

    import httpx
    with (
        patch("app.services.proxy.get_mock", AsyncMock(return_value=mock)),
        patch("app.services.proxy.get_settings", AsyncMock(return_value=GlobalSettings())),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client_cls.return_value = mock_client

        from app.services.proxy import proxy_request
        resp = await proxy_request(r, "svc.jar", "slow/endpoint", request)

    assert resp.status_code == 504
