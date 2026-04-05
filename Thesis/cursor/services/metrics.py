"""Metrics Exporter — Prometheus text exposition (ТЗ, раздел «Metrics Exporter»).

Данные из Redis читаются через Pipeline: сначала реестры, затем все Hash/GET одним batch.
"""

from __future__ import annotations

from redis.asyncio import Redis

from models.host import HostStatus
from models.mock import MockStatus

_METRIC_HELP = (
    ("mockcontrol_mocks_total", "Total number of registered mocks."),
    ("mockcontrol_mocks_running", "Number of mocks in RUNNING state."),
    ("mockcontrol_mocks_errors", "Number of mocks in ERROR state."),
    ("mockcontrol_hosts_available", "Number of hosts in AVAILABLE state."),
    ("mockcontrol_proxy_requests_total", "Total proxied HTTP requests per mock."),
    (
        "mockcontrol_rate_limit_rejected_total",
        "Total requests rejected by rate limiter per mock.",
    ),
)


def _escape_label_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _parse_int(raw: str | None) -> int:
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


async def collect_metrics(redis: Redis) -> str:
    """Собирает метрики из Redis и возвращает тело ответа в Prometheus exposition format."""
    pipe = redis.pipeline(transaction=False)
    pipe.smembers("mocks:registry")
    pipe.smembers("hosts:registry")
    reg_results = await pipe.execute()
    mock_files = sorted(reg_results[0] or [])
    host_names = sorted(reg_results[1] or [])

    pipe2 = redis.pipeline(transaction=False)
    for filename in mock_files:
        pipe2.hgetall(f"mocks:{filename}")
    for hostname in host_names:
        pipe2.hget(f"hosts:{hostname}", "status")
    for filename in mock_files:
        pipe2.get(f"metrics:proxy_total:{filename}")
        pipe2.get(f"metrics:rejected_total:{filename}")

    n_m = len(mock_files)
    n_h = len(host_names)
    if n_m == 0 and n_h == 0:
        mock_hashes: list[dict[str, str]] = []
        host_statuses: list[str | None] = []
        proxy_vals: list[str | None] = []
        reject_vals: list[str | None] = []
    else:
        batch = await pipe2.execute()
        mock_hashes = batch[:n_m]
        host_statuses = batch[n_m : n_m + n_h]
        metrics_slice = batch[n_m + n_h :]
        proxy_vals = metrics_slice[0::2]
        reject_vals = metrics_slice[1::2]

    mocks_total = n_m
    running = 0
    errors = 0
    for h in mock_hashes:
        st = (h.get("status") or "").strip()
        if st == MockStatus.RUNNING.value:
            running += 1
        elif st == MockStatus.ERROR.value:
            errors += 1

    hosts_available = sum(
        1 for s in host_statuses if (s or "").strip() == HostStatus.AVAILABLE.value
    )

    lines: list[str] = []
    for name, help_text in _METRIC_HELP:
        lines.append(f"# HELP {name} {help_text}")
        if name in (
            "mockcontrol_proxy_requests_total",
            "mockcontrol_rate_limit_rejected_total",
        ):
            lines.append(f"# TYPE {name} counter")
        else:
            lines.append(f"# TYPE {name} gauge")

    lines.append(f"mockcontrol_mocks_total {mocks_total}")
    lines.append(f"mockcontrol_mocks_running {running}")
    lines.append(f"mockcontrol_mocks_errors {errors}")
    lines.append(f"mockcontrol_hosts_available {hosts_available}")

    for filename, proxy_raw, rej_raw in zip(mock_files, proxy_vals, reject_vals):
        esc = _escape_label_value(filename)
        lines.append(
            f'mockcontrol_proxy_requests_total{{mock="{esc}"}} {_parse_int(proxy_raw)}'
        )
        lines.append(
            f'mockcontrol_rate_limit_rejected_total{{mock="{esc}"}} {_parse_int(rej_raw)}'
        )

    return "\n".join(lines) + "\n"
