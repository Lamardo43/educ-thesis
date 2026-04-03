"""
Proxy Engine — прозрачный обратный прокси-сервер (Reverse Proxy).

Маршрутизирует входящие HTTP-запросы к целевым заглушкам,
сохраняя метод, заголовки и тело запроса. При активном Rate Limiter
проверяет лимит до передачи запроса.

Для асинхронных исходящих запросов используется httpx.AsyncClient
с пулом соединений для минимизации накладных расходов.
"""

import logging
import time
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import Response

from mockcontrol.core.rate_limiter import RateLimiter
from mockcontrol.models.mock import MockConfig, MockStatus
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

# Заголовки, которые не нужно копировать из оригинального запроса
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})


class ProxyEngine:
    """
    Прозрачное проксирование HTTP-запросов к имитационным сервисам.

    Алгоритм обработки запроса:
    1. Извлечь конфигурацию заглушки из Redis по mock_name.
    2. Проверить статус (RUNNING).
    3. Если rate_limit_enabled — проверить лимит.
    4. Асинхронно переслать запрос на адрес заглушки.
    5. Вернуть ответ заглушки без изменений.
    """

    def __init__(
        self,
        mock_service: MockService,
        settings_service: SettingsService,
        rate_limiter: RateLimiter,
        default_timeout: float = 10.0,
    ) -> None:
        self._mocks = mock_service
        self._settings = settings_service
        self._rate_limiter = rate_limiter
        self._default_timeout = default_timeout

        # Пул соединений httpx — переиспользуется для всех запросов
        self._client: Optional[httpx.AsyncClient] = None

    async def startup(self) -> None:
        """Инициализировать пул соединений при старте приложения."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._default_timeout),
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
            ),
        )

    async def shutdown(self) -> None:
        """Закрыть пул соединений при завершении приложения."""
        if self._client:
            await self._client.aclose()

    async def handle_request(
        self,
        mock_name: str,
        path: str,
        request: Request,
    ) -> Response:
        """
        Обработать входящий запрос к заглушке.

        Args:
            mock_name: Имя файла заглушки (первый сегмент URL).
            path: Оставшийся путь после имени заглушки.
            request: Объект входящего запроса FastAPI.

        Returns:
            HTTP-ответ от заглушки или ответ с ошибкой (503, 429, 502).
        """
        t0 = time.monotonic()

        # Шаг 1: получить конфигурацию заглушки
        mock_config = await self._mocks.get(mock_name)
        t_redis_ms = (time.monotonic() - t0) * 1000

        if mock_config is None:
            return Response(
                content=f"Mock '{mock_name}' not found",
                status_code=404,
            )

        # Шаг 2: проверить статус
        if mock_config.status != MockStatus.RUNNING:
            return Response(
                content=f"Mock '{mock_name}' is not running (status: {mock_config.status})",
                status_code=503,
            )

        if mock_config.port is None:
            return Response(
                content=f"Mock '{mock_name}' has no assigned port",
                status_code=503,
            )

        # Шаг 3: проверить Rate Limiter (если включён)
        t_rl_ms = 0.0
        if mock_config.rate_limit_enabled and mock_config.rate_limit > 0:
            t_rl0 = time.monotonic()
            global_settings = await self._settings.get()
            result = await self._rate_limiter.check(
                filename=mock_name,
                limit=mock_config.rate_limit,
                window_size=global_settings.rate_limit_window_size,
            )
            t_rl_ms = (time.monotonic() - t_rl0) * 1000
            if not result.allowed:
                return Response(
                    content="Too Many Requests",
                    status_code=429,
                    headers={
                        "X-RateLimit-Limit": str(mock_config.rate_limit),
                        "X-RateLimit-Current": str(result.current_count),
                        "Retry-After": "1",
                    },
                )

        # Шаг 4: переслать запрос заглушке
        target_url = f"http://{mock_config.hostname}:{mock_config.port}/{path}"

        # Собрать заголовки, исключая hop-by-hop
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS
        }

        # Прочитать тело запроса
        body = await request.body()

        t_http0 = time.monotonic()
        try:
            proxy_response = await self._client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                params=dict(request.query_params),
            )
        except httpx.TimeoutException:
            logger.warning(
                "Proxy timeout for %s -> %s",
                mock_name, target_url,
            )
            return Response(
                content="Gateway Timeout",
                status_code=504,
            )
        except httpx.ConnectError:
            logger.error(
                "Proxy connection error for %s -> %s",
                mock_name, target_url,
            )
            return Response(
                content=f"Cannot connect to mock '{mock_name}' at {target_url}",
                status_code=502,
            )
        except httpx.HTTPError as exc:
            logger.error(
                "Proxy HTTP error for %s: %s",
                mock_name, exc,
            )
            return Response(
                content="Bad Gateway",
                status_code=502,
            )
        t_http_ms = (time.monotonic() - t_http0) * 1000

        total_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "PROXY %s %s %s: redis=%.1f ms  rate_limit=%.1f ms  http=%.1f ms  total=%.1f ms",
            request.method, mock_name, path,
            t_redis_ms, t_rl_ms, t_http_ms, total_ms,
        )
        if t_redis_ms > 50:
            logger.warning(
                "SLOW redis lookup for '%s': %.1f ms", mock_name, t_redis_ms,
            )
        if t_http_ms > 200:
            logger.warning(
                "SLOW upstream response from '%s' at %s: %.1f ms",
                mock_name, target_url, t_http_ms,
            )

        # Шаг 5: вернуть ответ заглушки без изменений
        response_headers = {
            key: value
            for key, value in proxy_response.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS
        }

        return Response(
            content=proxy_response.content,
            status_code=proxy_response.status_code,
            headers=response_headers,
            media_type=proxy_response.headers.get("content-type"),
        )
