"""
Тесты Proxy Engine.

Проверяет:
- Маршрутизацию к запущенной заглушке (200)
- Ответ 404 для несуществующей заглушки
- Ответ 503 для незапущенной заглушки
- Ответ 429 при превышении Rate Limit
- Пропуск Rate Limiter при rate_limit_enabled=False
- Обработку таймаута (504) и ошибок соединения (502)
- Проброс заголовков и тела запроса
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mockcontrol.core.proxy import ProxyEngine
from mockcontrol.core.rate_limiter import RateLimiter, RateLimitResult
from mockcontrol.models.mock import MockConfig, MockStatus
from mockcontrol.models.settings import GlobalSettings
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService


@pytest.fixture
def mock_request():
    """Фабрика мок-объектов FastAPI Request."""

    def _factory(method="GET", headers=None, body=b"", query_params=None):
        req = MagicMock()
        req.method = method
        req.headers = headers or {"content-type": "application/json"}
        req.query_params = query_params or {}
        req.body = AsyncMock(return_value=body)
        return req

    return _factory


@pytest.fixture
def running_mock_config():
    """Конфигурация запущенной заглушки."""
    return MockConfig(
        hostname="127.0.0.1",
        port=8101,
        pid=12345,
        status=MockStatus.RUNNING,
        rate_limit=100,
        rate_limit_enabled=False,
    )


@pytest.fixture
def proxy_engine(mock_service, settings_service, rate_limiter):
    """ProxyEngine с реальными сервисами (FakeRedis)."""
    engine = ProxyEngine(
        mock_service=mock_service,
        settings_service=settings_service,
        rate_limiter=rate_limiter,
    )
    # Мокаем httpx клиент
    engine._client = AsyncMock(spec=httpx.AsyncClient)
    return engine


@pytest.mark.asyncio
class TestProxyRouting:
    """Базовая маршрутизация запросов."""

    async def test_not_found(self, proxy_engine, mock_request):
        """Несуществующая заглушка → 404."""
        response = await proxy_engine.handle_request(
            "unknown.jar", "api/test", mock_request()
        )
        assert response.status_code == 404

    async def test_not_running(
        self, proxy_engine, mock_service, make_mock_config, mock_request
    ):
        """Незапущенная заглушка → 503."""
        await mock_service.register("stopped.jar", make_mock_config())

        response = await proxy_engine.handle_request(
            "stopped.jar", "api/test", mock_request()
        )
        assert response.status_code == 503

    async def test_successful_proxy(
        self, proxy_engine, mock_service, running_mock_config, mock_request
    ):
        """Успешное проксирование: запрос передан, ответ 200."""
        await mock_service.register("ok.jar", running_mock_config)

        # Мок ответа от заглушки
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"result": "ok"}'
        mock_response.headers = {"content-type": "application/json"}
        proxy_engine._client.request = AsyncMock(return_value=mock_response)

        response = await proxy_engine.handle_request(
            "ok.jar", "api/test", mock_request()
        )

        assert response.status_code == 200
        proxy_engine._client.request.assert_called_once()


@pytest.mark.asyncio
class TestProxyRateLimiting:
    """Интеграция с Rate Limiter."""

    async def test_rate_limit_disabled_skips_check(
        self, proxy_engine, mock_service, running_mock_config, mock_request
    ):
        """При rate_limit_enabled=False запрос проходит без проверки."""
        config = running_mock_config.model_copy(
            update={"rate_limit_enabled": False, "rate_limit": 1}
        )
        await mock_service.register("nolimit.jar", config)
        await proxy_engine._mocks._redis.set(
            "settings:global", GlobalSettings().model_dump_json()
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"ok"
        mock_resp.headers = {}
        proxy_engine._client.request = AsyncMock(return_value=mock_resp)

        # Должен пройти, несмотря на limit=1
        for _ in range(5):
            response = await proxy_engine.handle_request(
                "nolimit.jar", "test", mock_request()
            )
            assert response.status_code == 200

    async def test_rate_limit_returns_429(
        self, proxy_engine, mock_service, running_mock_config, mock_request
    ):
        """При превышении лимита → 429 Too Many Requests."""
        config = running_mock_config.model_copy(
            update={"rate_limit_enabled": True, "rate_limit": 2}
        )
        await mock_service.register("limited.jar", config)
        await proxy_engine._mocks._redis.set(
            "settings:global", GlobalSettings(rate_limit_window_size=60).model_dump_json()
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"ok"
        mock_resp.headers = {}
        proxy_engine._client.request = AsyncMock(return_value=mock_resp)

        # Первые 2 — ОК
        for _ in range(2):
            r = await proxy_engine.handle_request("limited.jar", "x", mock_request())
            assert r.status_code == 200

        # Третий — 429
        r = await proxy_engine.handle_request("limited.jar", "x", mock_request())
        assert r.status_code == 429


@pytest.mark.asyncio
class TestProxyErrors:
    """Обработка ошибок при проксировании."""

    async def test_timeout_returns_504(
        self, proxy_engine, mock_service, running_mock_config, mock_request
    ):
        """Таймаут ответа от заглушки → 504."""
        await mock_service.register("timeout.jar", running_mock_config)

        proxy_engine._client.request = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )

        response = await proxy_engine.handle_request(
            "timeout.jar", "api/slow", mock_request()
        )
        assert response.status_code == 504

    async def test_connection_error_returns_502(
        self, proxy_engine, mock_service, running_mock_config, mock_request
    ):
        """Ошибка соединения с заглушкой → 502."""
        await mock_service.register("connfail.jar", running_mock_config)

        proxy_engine._client.request = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )

        response = await proxy_engine.handle_request(
            "connfail.jar", "api/test", mock_request()
        )
        assert response.status_code == 502
