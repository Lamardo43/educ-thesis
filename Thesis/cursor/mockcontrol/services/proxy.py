"""Реестр httpx-клиентов и проксирование HTTP-запросов."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from httpx import Timeout

_DEFAULT_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)


class ProxyClientRegistry:
    """Хранит ``AsyncClient`` на имя мока (один клиент на ``base_url`` с пулом соединений)."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        mock_name: str,
        base_url: str,
        timeout: float | Timeout,
    ) -> httpx.AsyncClient:
        async with self._lock:
            existing = self._clients.get(mock_name)
            if existing is not None:
                return existing
            client = httpx.AsyncClient(
                base_url=base_url,
                timeout=timeout,
                limits=_DEFAULT_LIMITS,
                follow_redirects=False,
            )
            self._clients[mock_name] = client
            return client

    async def remove(self, mock_name: str) -> None:
        async with self._lock:
            client = self._clients.pop(mock_name, None)
        if client is not None:
            await client.aclose()

    async def close_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for c in clients:
            await c.aclose()


def _strip_hop_by_hop_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in ("host", "content-length"):
            continue
        out[key] = value
    return out


def _build_request_path(path: str, query_string: str | None) -> str:
    path_part = "/" if not path else path
    if not path_part.startswith("/"):
        path_part = "/" + path_part
    if query_string:
        return f"{path_part}?{query_string}"
    return path_part


async def proxy_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: Mapping[str, str] | None,
    body: bytes | None,
    query_string: str | None,
) -> tuple[int, dict[str, str], bytes]:
    """
    Пересылает запрос через ``client`` (``base_url`` уже задан у клиента).

    Возвращает ``(status_code, response_headers, response_body)``.
    При сетевой ошибке — ``502``, при таймауте — ``504`` (тело ответа пустое).
    """
    url = _build_request_path(path, query_string)
    req_headers = _strip_hop_by_hop_headers(headers)
    try:
        response = await client.request(
            method.upper(),
            url,
            headers=req_headers,
            content=body if body is not None else None,
        )
    except httpx.TimeoutException:
        return (504, {}, b"")
    except httpx.RequestError:
        return (502, {}, b"")

    resp_headers = {k: v for k, v in response.headers.items()}
    return (response.status_code, resp_headers, response.content)
