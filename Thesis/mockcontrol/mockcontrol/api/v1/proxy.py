"""
Catch-all роут прозрачного проксирования.

Перехватывает все HTTP-запросы вида /{mock_name}/{path}
и маршрутизирует их к соответствующей заглушке через Proxy Engine.

Этот роутер должен быть подключён ПОСЛЕДНИМ, чтобы не перехватывать
запросы к /api/*, /metrics и другим служебным эндпоинтам.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from mockcontrol.core.proxy import ProxyEngine
from mockcontrol.dependencies import get_proxy

router = APIRouter(tags=["proxy"])


@router.api_route(
    "/{mock_name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    summary="Проксирование запроса к заглушке",
    include_in_schema=False,
)
async def proxy_request(
    mock_name: str,
    path: str,
    request: Request,
    proxy: Annotated[ProxyEngine, Depends(get_proxy)],
) -> Response:
    """
    Прозрачное проксирование HTTP-запроса к целевой заглушке.

    Алгоритм:
    1. Извлечь конфигурацию заглушки из Redis по mock_name.
    2. Проверить статус (RUNNING) и доступность порта.
    3. При активном Rate Limiter — проверить лимит.
    4. Переслать запрос через httpx на {hostname}:{port}/{path}.
    5. Вернуть ответ заглушки без изменений.

    Коды ответов системы (не от заглушки):
    - 404 — заглушка не найдена
    - 429 — превышен лимит запросов (Rate Limiter)
    - 502 — ошибка соединения с заглушкой
    - 503 — заглушка не запущена
    - 504 — таймаут ответа от заглушки
    """
    return await proxy.handle_request(mock_name, path, request)


@router.api_route(
    "/{mock_name}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    summary="Проксирование запроса к корню заглушки",
    include_in_schema=False,
)
async def proxy_request_root(
    mock_name: str,
    request: Request,
    proxy: Annotated[ProxyEngine, Depends(get_proxy)],
) -> Response:
    """Проксирование запроса к корневому пути заглушки (без суффикса)."""
    return await proxy.handle_request(mock_name, "", request)
