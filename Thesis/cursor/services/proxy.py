"""Proxy Engine — реестр httpx.AsyncClient и проксирование HTTP (ТЗ, раздел «Proxy Engine»).

Один AsyncClient на заглушку с пулом соединений и keep-alive; прозрачная пересылка метода,
заголовков (кроме Host), тела и query string.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping

import httpx

_STRIP_REQUEST_HEADERS = frozenset({"host", "content-length"})
_PROXY_MAX_CONNECTIONS = 1000
_PROXY_MAX_KEEPALIVE_CONNECTIONS = 200
_PROXY_KEEPALIVE_EXPIRY_SEC = 60.0

logger = logging.getLogger(__name__)


def _header_items(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> Iterable[tuple[str, str]]:
    if isinstance(headers, Mapping):
        return headers.items()
    return headers


def _filtered_request_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _header_items(headers):
        if key.lower() in _STRIP_REQUEST_HEADERS:
            continue
        out[key] = value
    return out


def _request_path(path: str, query_string: str) -> str:
    url_path = "/" if not path else "/" + path.lstrip("/")
    if query_string:
        return f"{url_path}?{query_string}"
    return url_path


class ProxyClientRegistry:
    """Глобальный реестр httpx.AsyncClient по имени заглушки (mock_name)."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        mock_name: str,
        base_url: str,
        timeout: float | httpx.Timeout,
    ) -> httpx.AsyncClient:
        async with self._lock:
            existing = self._clients.get(mock_name)
            if existing is not None:
                return existing
            limits = httpx.Limits(
                max_connections=_PROXY_MAX_CONNECTIONS,
                max_keepalive_connections=_PROXY_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=_PROXY_KEEPALIVE_EXPIRY_SEC,
            )
            t = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
            base = base_url.rstrip("/")
            client = httpx.AsyncClient(
                base_url=base,
                timeout=t,
                limits=limits,
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
            to_close = list(self._clients.values())
            self._clients.clear()
        for client in to_close:
            await client.aclose()


async def proxy_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    body: bytes | None,
    query_string: str,
) -> tuple[int, dict[str, str], bytes]:
    """Пересылает запрос через клиент; возвращает статус, заголовки ответа и тело.

    При пустом ``path`` запрос уходит на ``/`` относительно ``base_url`` клиента.
    Ошибки транспорта: таймауты → 504, прочие сбои соединения/чтения → 502.
    """
    target = _request_path(path, query_string)
    hdrs = _filtered_request_headers(headers)
    req_kwargs: dict[str, object] = {}
    if body is not None:
        req_kwargs["content"] = body
    started = time.perf_counter()
    try:
        t0 = time.perf_counter()
        response = await client.request(
            method.upper(),
            target,
            headers=hdrs,
            **req_kwargs,
        )
        request_ms = (time.perf_counter() - t0) * 1000.0
    except httpx.TimeoutException:
        total_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "proxy_upstream_timing method=%s target=%s status=timeout request_ms=%.2f total_ms=%.2f",
            method.upper(),
            target,
            total_ms,
            total_ms,
        )
        return (504, {}, b"")
    except httpx.RequestError:
        total_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "proxy_upstream_timing method=%s target=%s status=request_error request_ms=%.2f total_ms=%.2f",
            method.upper(),
            target,
            total_ms,
            total_ms,
        )
        return (502, {}, b"")
    try:
        t0 = time.perf_counter()
        raw = await response.aread()
        body_read_ms = (time.perf_counter() - t0) * 1000.0
        total_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "proxy_upstream_timing method=%s target=%s status=%s request_ms=%.2f body_read_ms=%.2f total_ms=%.2f",
            method.upper(),
            target,
            response.status_code,
            request_ms,
            body_read_ms,
            total_ms,
        )
        return (response.status_code, dict(response.headers), raw)
    finally:
        await response.aclose()
