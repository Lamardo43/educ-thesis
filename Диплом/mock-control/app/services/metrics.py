"""
MetricsExporter — assembles Prometheus metrics from Redis data.

Exposed metrics:
  mock_rps_current{mock="..."}          — current requests per second
  mock_rate_limit_active{mock="..."}    — 1 if rate limiting is enabled
  mock_status_running{mock="..."}       — 1 if status == RUNNING
  mock_rate_limit_threshold{mock="..."} — configured RPS limit
  host_available{host="..."}            — 1 if SSH status == AVAILABLE
"""
import math
import time
import logging

import redis.asyncio as aioredis

from app.models.mock import MockStatus
from app.models.host import HostStatus
from app.repositories.mock_repo import list_mocks
from app.repositories.host_repo import list_hosts
from app.repositories.settings_repo import get_settings

logger = logging.getLogger(__name__)


async def generate_metrics(r: aioredis.Redis) -> str:
    settings = await get_settings(r)
    window_size = settings.rate_limit_window_size
    window = math.floor(time.time() / window_size)

    mocks = await list_mocks(r)
    hosts = await list_hosts(r)

    lines: list[str] = []

    # ── mock_status_running ────────────────────────────────────────────────
    lines.append("# HELP mock_status_running 1 if the mock process is running")
    lines.append("# TYPE mock_status_running gauge")
    for m in mocks:
        val = 1 if m.status == MockStatus.RUNNING else 0
        lines.append(f'mock_status_running{{mock="{m.filename}"}} {val}')

    # ── mock_rps_current ───────────────────────────────────────────────────
    lines.append("# HELP mock_rps_current Requests in the current rate-limit window")
    lines.append("# TYPE mock_rps_current gauge")

    # Fetch all counters in one pipeline
    pipe = r.pipeline()
    for m in mocks:
        pipe.get(f"rate:{m.filename}:{window}")
    rps_values = await pipe.execute()

    for m, val in zip(mocks, rps_values):
        rps = int(val) if val else 0
        lines.append(f'mock_rps_current{{mock="{m.filename}"}} {rps}')

    # ── mock_rate_limit_active ─────────────────────────────────────────────
    lines.append("# HELP mock_rate_limit_active 1 if rate limiting is enabled for the mock")
    lines.append("# TYPE mock_rate_limit_active gauge")
    for m in mocks:
        val = 1 if m.rate_limit_enabled else 0
        lines.append(f'mock_rate_limit_active{{mock="{m.filename}"}} {val}')

    # ── mock_rate_limit_threshold ──────────────────────────────────────────
    lines.append("# HELP mock_rate_limit_threshold Configured RPS limit for the mock")
    lines.append("# TYPE mock_rate_limit_threshold gauge")
    for m in mocks:
        lines.append(f'mock_rate_limit_threshold{{mock="{m.filename}"}} {m.rate_limit}')

    # ── host_available ─────────────────────────────────────────────────────
    lines.append("# HELP host_available 1 if the host is reachable via SSH")
    lines.append("# TYPE host_available gauge")
    for h in hosts:
        val = 1 if h.status == HostStatus.AVAILABLE else 0
        lines.append(f'host_available{{host="{h.hostname}"}} {val}')

    return "\n".join(lines) + "\n"
