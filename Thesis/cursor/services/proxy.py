"""Proxy Engine — реестр httpx.AsyncClient и проксирование HTTP (ТЗ, раздел «Proxy Engine»).

Один AsyncClient на заглушку с пулом соединений и keep-alive; прозрачная пересылка метода,
заголовков (кроме Host), тела и query string.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import httpx

_STRIP_REQUEST_HEADERS = frozenset({"host", "content-length"})
_PROXY_MAX_CONNECTIONS = 1000
_PROXY_MAX_KEEPALIVE_CONNECTIONS = 200
_PROXY_KEEPALIVE_EXPIRY_SEC = 60.0


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


@dataclass(slots=True)
class ProxyRequestObservation:
    """Диагностические метрики этапов запроса к upstream."""

    timings_ms: dict[str, float]
    outcome: str
    upstream_status: int | None


class ProxyClientRegistry:
    """Глобальный реестр httpx.AsyncClient по имени заглушки (mock_name).

    Горячий путь (клиент уже существует) не захватывает лок: в asyncio event loop
    dict.get() атомарен — настоящего параллелизма нет, гонок не возникает.
    Лок берётся только при создании нового клиента (double-checked locking).
    """

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        mock_name: str,
        base_url: str,
        timeout: float | httpx.Timeout,
    ) -> httpx.AsyncClient:
        # Быстрый путь — без лока. Безопасно: asyncio однопоточен,
        # dict.get() не прерывается между корутинами.
        existing = self._clients.get(mock_name)
        if existing is not None:
            return existing

        # Медленный путь — создание нового клиента. Лок защищает от
        # двойного создания при одновременных первых запросах к одной заглушке.
        async with self._lock:
            # Повторная проверка: пока мы ждали лок, другая корутина
            # могла уже создать клиента.
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
) -> tuple[int, dict[str, str], bytes, ProxyRequestObservation]:
    """Пересылает запрос через клиент; возвращает статус, заголовки ответа и тело.

    При пустом ``path`` запрос уходит на ``/`` относительно ``base_url`` клиента.
    Ошибки транспорта: таймауты → 504, прочие сбои соединения/чтения → 502.
    """
    target = _request_path(path, query_string)
    hdrs = _filtered_request_headers(headers)
    req_kwargs: dict[str, object] = {}
    if body is not None:
        req_kwargs["content"] = body

    started_at = time.perf_counter()
    network_timeline: dict[str, float] = {}
    trace_started: dict[str, float] = {}
    queue_wait_ms = 0.0
    queue_started_at: float | None = None

    def _mark(stage: str, stage_started_at: float) -> None:
        network_timeline[stage] = network_timeline.get(stage, 0.0) + (
            (time.perf_counter() - stage_started_at) * 1000.0
        )

    async def _trace(event_name: str, info: dict[str, object]) -> None:
        del info
        nonlocal queue_wait_ms, queue_started_at
        now = time.perf_counter()
        if event_name == "connection_pool.wait_for_connection.started":
            queue_started_at = now
        elif event_name == "connection_pool.wait_for_connection.complete":
            if queue_started_at is not None:
                queue_wait_ms += (now - queue_started_at) * 1000.0
                queue_started_at = None
        if event_name.endswith(".started"):
            trace_started[event_name[: -len(".started")]] = now
            return
        if event_name.endswith(".complete"):
            base_name = event_name[: -len(".complete")]
            started = trace_started.pop(base_name, None)
            if started is not None:
                _mark(f"httpx_{base_name}_ms", started)

    req_kwargs["extensions"] = {"trace": _trace}
    response: httpx.Response | None = None
    outcome = "ok"
    upstream_status: int | None = None
    status_code = 502
    resp_headers: dict[str, str] = {}
    resp_body = b""
    try:
        t = time.perf_counter()
        response = await client.request(
            method.upper(),
            target,
            headers=hdrs,
            **req_kwargs,
        )
        _mark("httpx_client_request_ms", t)

        t = time.perf_counter()
        raw = await response.aread()
        _mark("httpx_response_read_ms", t)
        upstream_status = response.status_code
        status_code = response.status_code
        resp_headers = dict(response.headers)
        resp_body = raw
    except httpx.TimeoutException:
        outcome = "timeout"
        status_code = 504
        resp_headers = {}
        resp_body = b""
    except httpx.RequestError:
        outcome = "request_error"
        status_code = 502
        resp_headers = {}
        resp_body = b""
    finally:
        if queue_started_at is not None:
            queue_wait_ms += (time.perf_counter() - queue_started_at) * 1000.0
            queue_started_at = None
        network_timeline["httpx_pool_wait_ms"] = round(queue_wait_ms, 3)
        httpx_total_ms = (time.perf_counter() - started_at) * 1000.0
        network_timeline["httpx_total_ms"] = round(httpx_total_ms, 3)
        if response is not None:
            await response.aclose()
    return (
        status_code,
        resp_headers,
        resp_body,
        ProxyRequestObservation(
            timings_ms={k: round(v, 3) for k, v in network_timeline.items()},
            outcome=outcome,
            upstream_status=upstream_status,
        ),
    )