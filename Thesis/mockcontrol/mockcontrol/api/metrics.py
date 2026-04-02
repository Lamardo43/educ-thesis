"""
Эндпоинт экспорта метрик в Prometheus exposition format.

Расположен вне /api/v1/ — по стандартному пути /metrics,
который ожидает Prometheus при скрейпинге.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from mockcontrol.core.metrics import MetricsExporter
from mockcontrol.dependencies import get_metrics

router = APIRouter(tags=["monitoring"])


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Метрики Prometheus",
)
async def metrics_endpoint(
    exporter: Annotated[MetricsExporter, Depends(get_metrics)],
) -> PlainTextResponse:
    """
    Эндпоинт для сбора метрик системой Prometheus.

    Возвращает данные в Prometheus text-based exposition format:
    - mockcontrol_mocks_total — количество заглушек по статусам
    - mockcontrol_mock_info — информация о каждой заглушке
    - mockcontrol_mock_rps — текущий RPS
    - mockcontrol_hosts_total — количество хостов по статусам
    - mockcontrol_metrics_scrape_seconds — время генерации метрик
    """
    content = await exporter.export()
    return PlainTextResponse(
        content=content,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
