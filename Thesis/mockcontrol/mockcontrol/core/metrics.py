"""
Metrics Exporter — формирование метрик в Prometheus exposition format.

Агрегирует данные о заглушках, хостах и Rate Limiter из Redis
с использованием Pipeline для минимизации сетевых round-trip.

Метрики:
- mockcontrol_mocks_total{status} — количество заглушек по статусам
- mockcontrol_mock_info{filename,hostname,port,status} — информация о заглушке
- mockcontrol_mock_rps{filename} — текущий RPS (запросов за последнее окно)
- mockcontrol_rate_limit_enabled{filename} — флаг активности Rate Limiter
- mockcontrol_hosts_total{status} — количество хостов по статусам
- mockcontrol_host_info{hostname,status} — информация о хосте
"""

import logging
import time
from collections import Counter
from io import StringIO

from mockcontrol.core.rate_limiter import RateLimiter
from mockcontrol.models.host import HostStatus
from mockcontrol.models.mock import MockStatus
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


class MetricsExporter:
    """Генерация метрик в Prometheus text format."""

    def __init__(
        self,
        mock_service: MockService,
        host_service: HostService,
        settings_service: SettingsService,
        rate_limiter: RateLimiter,
    ) -> None:
        self._mocks = mock_service
        self._hosts = host_service
        self._settings = settings_service
        self._rate_limiter = rate_limiter

    async def export(self) -> str:
        """
        Собрать все метрики и вернуть в Prometheus exposition format.

        Все операции чтения Redis выполняются через Pipeline
        для минимизации задержек.
        """
        buf = StringIO()
        start = time.monotonic()

        # --- Метрики заглушек ---
        mock_configs = await self._mocks.list_configs()
        global_settings = await self._settings.get()

        # Подсчёт по статусам
        status_counts: Counter[str] = Counter()
        for config in mock_configs.values():
            status_counts[config.status.value] += 1

        buf.write("# HELP mockcontrol_mocks_total Number of registered mocks by status\n")
        buf.write("# TYPE mockcontrol_mocks_total gauge\n")
        for status in MockStatus:
            count = status_counts.get(status.value, 0)
            buf.write(f'mockcontrol_mocks_total{{status="{status.value}"}} {count}\n')

        # Информация о каждой заглушке
        buf.write("# HELP mockcontrol_mock_info Mock service information\n")
        buf.write("# TYPE mockcontrol_mock_info gauge\n")
        for filename, config in mock_configs.items():
            port_str = str(config.port) if config.port else "none"
            buf.write(
                f'mockcontrol_mock_info{{'
                f'filename="{filename}",'
                f'hostname="{config.hostname}",'
                f'port="{port_str}",'
                f'status="{config.status.value}"'
                f'}} 1\n'
            )

        # Rate Limiter
        buf.write("# HELP mockcontrol_rate_limit_enabled Rate limiter enabled flag\n")
        buf.write("# TYPE mockcontrol_rate_limit_enabled gauge\n")
        for filename, config in mock_configs.items():
            val = 1 if config.rate_limit_enabled else 0
            buf.write(
                f'mockcontrol_rate_limit_enabled{{filename="{filename}"}} {val}\n'
            )

        # Текущий RPS
        buf.write("# HELP mockcontrol_mock_rps Current requests per second\n")
        buf.write("# TYPE mockcontrol_mock_rps gauge\n")
        for filename, config in mock_configs.items():
            if config.status == MockStatus.RUNNING:
                rps = await self._rate_limiter.get_current_count(
                    filename,
                    window_size=global_settings.rate_limit_window_size,
                )
            else:
                rps = 0
            buf.write(f'mockcontrol_mock_rps{{filename="{filename}"}} {rps}\n')

        # --- Метрики хостов ---
        host_configs = await self._hosts.list_configs()

        host_status_counts: Counter[str] = Counter()
        for config in host_configs.values():
            host_status_counts[config.status.value] += 1

        buf.write("# HELP mockcontrol_hosts_total Number of hosts by status\n")
        buf.write("# TYPE mockcontrol_hosts_total gauge\n")
        for status in HostStatus:
            count = host_status_counts.get(status.value, 0)
            buf.write(f'mockcontrol_hosts_total{{status="{status.value}"}} {count}\n')

        buf.write("# HELP mockcontrol_host_info Host information\n")
        buf.write("# TYPE mockcontrol_host_info gauge\n")
        for hostname, config in host_configs.items():
            buf.write(
                f'mockcontrol_host_info{{'
                f'hostname="{hostname}",'
                f'status="{config.status.value}"'
                f'}} 1\n'
            )

        # --- Метрики самой системы ---
        elapsed = time.monotonic() - start
        buf.write("# HELP mockcontrol_metrics_scrape_seconds Time to generate metrics\n")
        buf.write("# TYPE mockcontrol_metrics_scrape_seconds gauge\n")
        buf.write(f"mockcontrol_metrics_scrape_seconds {elapsed:.6f}\n")

        return buf.getvalue()
