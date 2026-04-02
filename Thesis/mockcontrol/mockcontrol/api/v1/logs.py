"""
REST API для получения журналов (логов) Java-процессов заглушек.

Логи хранятся на целевых хостах в файлах {working_dir}/{filename}.log.
Чтение выполняется через SSH-команду `tail -n N`.

Эндпоинты:
    GET  /api/v1/logs/{mock_name}  — получить последние строки лога
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from mockcontrol.core.lifecycle import LifecycleError, LifecycleManager
from mockcontrol.dependencies import get_lifecycle

router = APIRouter(prefix="/logs", tags=["logs"])


class LogResponse(BaseModel):
    """Ответ с содержимым лога."""

    mock_name: str
    lines_requested: int
    content: str


@router.get(
    "/{mock_name}",
    response_model=LogResponse,
    summary="Получить логи заглушки",
)
async def get_logs(
    mock_name: str,
    lifecycle: Annotated[LifecycleManager, Depends(get_lifecycle)],
    lines: Annotated[int, Query(ge=1, le=5000, description="Количество строк")] = 200,
) -> LogResponse:
    """
    Получить последние N строк лога Java-процесса заглушки.

    Лог читается с целевого хоста через SSH (`tail -n`).
    Доступен вне зависимости от текущего статуса заглушки —
    файл лога сохраняется на хосте после остановки процесса.
    """
    try:
        content = await lifecycle.get_logs(mock_name, lines=lines)
    except LifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    return LogResponse(
        mock_name=mock_name,
        lines_requested=lines,
        content=content,
    )
