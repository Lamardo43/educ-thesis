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
    (
        "mockcontrol_proxy_stage_latency_ms_sum",
        "Sum of proxy stage latencies in milliseconds.",
    ),
    (
        "mockcontrol_proxy_stage_latency_ms_count",
        "Number of observations for proxy stage latencies.",
    ),
    (
        "mockcontrol_proxy_stage_latency_ms_avg",
        "Last observed proxy stage duration in milliseconds (per stage, not sum/count).",
    ),
    (
        "mockcontrol_proxy_stage_latency_ms_last",
        "Last observed latency for proxy stages in milliseconds.",
    ),
    (
        "mockcontrol_proxy_outcome_total",
        "Total proxy outcomes by result type.",
    ),
    (
        "mockcontrol_proxy_upstream_status_total",
        "Total upstream HTTP statuses returned by proxy.",
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


def _parse_float(raw: str | None) -> float:
    if raw is None or raw == "":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


async def record_proxy_observation(
    redis: Redis,
    mock_name: str,
    stage_timings_ms: dict[str, float],
    outcome: str,
    upstream_status: int | None,
) -> None:
    """Агрегирует тайминги/исходы проксирования в Redis для Prometheus-экспорта."""
    if not stage_timings_ms and not outcome and upstream_status is None:
        return

    per_sum = f"metrics:proxy_stage_sum_ms:{mock_name}"
    per_cnt = f"metrics:proxy_stage_count:{mock_name}"
    per_last = f"metrics:proxy_stage_last_ms:{mock_name}"
    all_sum = "metrics:proxy_stage_sum_ms:__all__"
    all_cnt = "metrics:proxy_stage_count:__all__"
    all_last = "metrics:proxy_stage_last_ms:__all__"

    pipe = redis.pipeline(transaction=False)
    for stage, raw_ms in stage_timings_ms.items():
        ms = round(max(0.0, float(raw_ms)), 3)
        pipe.hincrbyfloat(per_sum, stage, ms)
        pipe.hincrby(per_cnt, stage, 1)
        pipe.hset(per_last, stage, ms)
        pipe.hincrbyfloat(all_sum, stage, ms)
        pipe.hincrby(all_cnt, stage, 1)
        pipe.hset(all_last, stage, ms)

    if outcome:
        pipe.hincrby(f"metrics:proxy_outcome_total:{mock_name}", outcome, 1)
        pipe.hincrby("metrics:proxy_outcome_total:__all__", outcome, 1)

    if upstream_status is not None:
        status = str(upstream_status)
        pipe.hincrby(f"metrics:proxy_upstream_status_total:{mock_name}", status, 1)
        pipe.hincrby("metrics:proxy_upstream_status_total:__all__", status, 1)

    await pipe.execute()


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
        pipe2.hgetall(f"metrics:proxy_stage_sum_ms:{filename}")
        pipe2.hgetall(f"metrics:proxy_stage_count:{filename}")
        pipe2.hgetall(f"metrics:proxy_stage_last_ms:{filename}")
        pipe2.hgetall(f"metrics:proxy_outcome_total:{filename}")
        pipe2.hgetall(f"metrics:proxy_upstream_status_total:{filename}")

    pipe2.hgetall("metrics:proxy_stage_sum_ms:__all__")
    pipe2.hgetall("metrics:proxy_stage_count:__all__")
    pipe2.hgetall("metrics:proxy_stage_last_ms:__all__")
    pipe2.hgetall("metrics:proxy_outcome_total:__all__")
    pipe2.hgetall("metrics:proxy_upstream_status_total:__all__")

    n_m = len(mock_files)
    n_h = len(host_names)
    batch = await pipe2.execute()
    mock_hashes = batch[:n_m]
    host_statuses = batch[n_m : n_m + n_h]
    metrics_slice = batch[n_m + n_h :]
    proxy_vals: list[str | None] = []
    reject_vals: list[str | None] = []
    stage_sums: list[dict[str, str]] = []
    stage_counts: list[dict[str, str]] = []
    stage_lasts: list[dict[str, str]] = []
    outcome_maps: list[dict[str, str]] = []
    upstream_maps: list[dict[str, str]] = []

    idx = 0
    for _ in mock_files:
        proxy_vals.append(metrics_slice[idx])
        idx += 1
        reject_vals.append(metrics_slice[idx])
        idx += 1
        stage_sums.append(metrics_slice[idx] or {})
        idx += 1
        stage_counts.append(metrics_slice[idx] or {})
        idx += 1
        stage_lasts.append(metrics_slice[idx] or {})
        idx += 1
        outcome_maps.append(metrics_slice[idx] or {})
        idx += 1
        upstream_maps.append(metrics_slice[idx] or {})
        idx += 1

    all_stage_sum: dict[str, str] = metrics_slice[idx] or {}
    idx += 1
    all_stage_count: dict[str, str] = metrics_slice[idx] or {}
    idx += 1
    all_stage_last: dict[str, str] = metrics_slice[idx] or {}
    idx += 1
    all_outcomes: dict[str, str] = metrics_slice[idx] or {}
    idx += 1
    all_upstream: dict[str, str] = metrics_slice[idx] or {}

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
            "mockcontrol_proxy_stage_latency_ms_sum",
            "mockcontrol_proxy_stage_latency_ms_count",
            "mockcontrol_proxy_outcome_total",
            "mockcontrol_proxy_upstream_status_total",
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

    for filename, sums_map, cnt_map, last_map in zip(
        mock_files, stage_sums, stage_counts, stage_lasts
    ):
        esc = _escape_label_value(filename)
        for stage, sum_raw in sorted(sums_map.items()):
            stage_esc = _escape_label_value(stage)
            sum_v = _parse_float(sum_raw)
            cnt_v = _parse_int(cnt_map.get(stage))
            lines.append(
                f'mockcontrol_proxy_stage_latency_ms_sum{{mock="{esc}",stage="{stage_esc}"}} {sum_v}'
            )
            lines.append(
                f'mockcontrol_proxy_stage_latency_ms_count{{mock="{esc}",stage="{stage_esc}"}} {cnt_v}'
            )
            last_ms = _parse_float(last_map.get(stage))
            lines.append(
                f'mockcontrol_proxy_stage_latency_ms_avg{{mock="{esc}",stage="{stage_esc}"}} {round(last_ms, 6)}'
            )
            lines.append(
                f'mockcontrol_proxy_stage_latency_ms_last{{mock="{esc}",stage="{stage_esc}"}} {last_ms}'
            )

    for stage, sum_raw in sorted(all_stage_sum.items()):
        stage_esc = _escape_label_value(stage)
        sum_v = _parse_float(sum_raw)
        cnt_v = _parse_int(all_stage_count.get(stage))
        lines.append(
            f'mockcontrol_proxy_stage_latency_ms_sum{{mock="__all__",stage="{stage_esc}"}} {sum_v}'
        )
        lines.append(
            f'mockcontrol_proxy_stage_latency_ms_count{{mock="__all__",stage="{stage_esc}"}} {cnt_v}'
        )
        last_ms = _parse_float(all_stage_last.get(stage))
        lines.append(
            f'mockcontrol_proxy_stage_latency_ms_avg{{mock="__all__",stage="{stage_esc}"}} {round(last_ms, 6)}'
        )
        lines.append(
            f'mockcontrol_proxy_stage_latency_ms_last{{mock="__all__",stage="{stage_esc}"}} {last_ms}'
        )

    for filename, outcomes in zip(mock_files, outcome_maps):
        esc = _escape_label_value(filename)
        for outcome, count_raw in sorted(outcomes.items()):
            outcome_esc = _escape_label_value(outcome)
            lines.append(
                f'mockcontrol_proxy_outcome_total{{mock="{esc}",outcome="{outcome_esc}"}} {_parse_int(count_raw)}'
            )

    for outcome, count_raw in sorted(all_outcomes.items()):
        outcome_esc = _escape_label_value(outcome)
        lines.append(
            f'mockcontrol_proxy_outcome_total{{mock="__all__",outcome="{outcome_esc}"}} {_parse_int(count_raw)}'
        )

    for filename, statuses in zip(mock_files, upstream_maps):
        esc = _escape_label_value(filename)
        for status, count_raw in sorted(statuses.items()):
            status_esc = _escape_label_value(status)
            lines.append(
                f'mockcontrol_proxy_upstream_status_total{{mock="{esc}",status="{status_esc}"}} {_parse_int(count_raw)}'
            )

    for status, count_raw in sorted(all_upstream.items()):
        status_esc = _escape_label_value(status)
        lines.append(
            f'mockcontrol_proxy_upstream_status_total{{mock="__all__",status="{status_esc}"}} {_parse_int(count_raw)}'
        )

    return "\n".join(lines) + "\n"
