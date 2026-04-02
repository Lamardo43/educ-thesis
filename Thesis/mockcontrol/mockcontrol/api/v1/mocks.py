"""
REST API для управления имитационными сервисами (заглушками).

Эндпоинты:
    GET    /api/v1/mocks                         — список всех заглушек
    GET    /api/v1/mocks/{mock_name}              — детали заглушки
    POST   /api/v1/mocks                         — регистрация (multipart upload)
    DELETE /api/v1/mocks/{mock_name}              — удаление
    POST   /api/v1/mocks/{mock_name}/start        — запуск
    POST   /api/v1/mocks/{mock_name}/stop         — остановка
    PATCH  /api/v1/mocks/{mock_name}/rate-limit   — переключение Rate Limiter
    PATCH  /api/v1/mocks/{mock_name}/jvm-args     — обновление JVM-аргументов
"""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from mockcontrol.config import settings
from mockcontrol.core.lifecycle import LifecycleError, LifecycleManager
from mockcontrol.core.rate_limiter import RateLimiter
from mockcontrol.dependencies import (
    get_lifecycle,
    get_mock_service,
    get_rate_limiter,
)
from mockcontrol.models.mock import MockConfig, MockRateLimitUpdate, MockStatus
from mockcontrol.services.mock_service import MockService
from mockcontrol.utils import validate_artifact_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mocks", tags=["mocks"])


# -------------------------------------------------------------------------
# Схемы ответов
# -------------------------------------------------------------------------

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class MockResponse(BaseModel):
    """Ответ с информацией о заглушке."""

    filename: str
    hostname: str
    port: Optional[int] = None
    pid: Optional[int] = None
    status: MockStatus
    jvm_args: str
    rate_limit: int
    rate_limit_enabled: bool
    registered_at: datetime
    started_at: Optional[datetime] = None


class MockListResponse(BaseModel):
    """Список заглушек."""

    total: int
    mocks: list[MockResponse]


class MessageResponse(BaseModel):
    """Простое сообщение."""

    message: str


class JvmArgsUpdate(BaseModel):
    """Запрос на обновление JVM-аргументов."""

    jvm_args: str


# -------------------------------------------------------------------------
# Вспомогательные функции
# -------------------------------------------------------------------------


def _to_response(filename: str, config: MockConfig) -> MockResponse:
    """Преобразовать внутреннюю модель в ответ API."""
    return MockResponse(
        filename=filename,
        hostname=config.hostname,
        port=config.port,
        pid=config.pid,
        status=config.status,
        jvm_args=config.jvm_args,
        rate_limit=config.rate_limit,
        rate_limit_enabled=config.rate_limit_enabled,
        registered_at=config.registered_at,
        started_at=config.started_at,
    )


# -------------------------------------------------------------------------
# Эндпоинты
# -------------------------------------------------------------------------


@router.get(
    "",
    response_model=MockListResponse,
    summary="Получить список всех заглушек",
)
async def list_mocks(
    mock_service: Annotated[MockService, Depends(get_mock_service)],
) -> MockListResponse:
    """
    Получить полный список зарегистрированных заглушек
    с их текущими статусами.
    """
    configs = await mock_service.list_configs()
    mocks = [_to_response(fn, cfg) for fn, cfg in configs.items()]
    return MockListResponse(total=len(mocks), mocks=mocks)


@router.get(
    "/{mock_name}",
    response_model=MockResponse,
    summary="Получить информацию о заглушке",
)
async def get_mock(
    mock_name: str,
    mock_service: Annotated[MockService, Depends(get_mock_service)],
) -> MockResponse:
    """Получить детальную информацию о конкретной заглушке."""
    config = await mock_service.get(mock_name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mock '{mock_name}' not found",
        )
    return _to_response(mock_name, config)


@router.post(
    "",
    response_model=MockResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать новую заглушку",
)
async def register_mock(
    file: Annotated[UploadFile, File(description="Файл артефакта (.jar / .war)")],
    hostname: Annotated[str, Form(description="Целевой хост из реестра")],
    lifecycle: Annotated[LifecycleManager, Depends(get_lifecycle)],
    jvm_args: Annotated[str, Form(description="JVM-аргументы")] = "",
    rate_limit: Annotated[int, Form(description="Макс. RPS (0 = без ограничений)", ge=0)] = 0,
    start_immediately: Annotated[bool, Form(description="Запустить сразу после регистрации")] = False,
) -> MockResponse:
    """
    Зарегистрировать новую заглушку.

    Принимает бинарный файл (.jar / .war) через multipart/form-data.
    Файл копируется на целевой хост через SCP, после чего
    временная копия на сервере управляющего приложения удаляется.
    """
    filename = file.filename or "unknown.jar"

    # Валидация имени файла
    error = validate_artifact_filename(filename)
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Сохранение во временный каталог
    tmp_path = settings.tmp_upload_dir / filename
    settings.tmp_upload_dir.mkdir(parents=True, exist_ok=True)

    try:
        content = await file.read()
        tmp_path.write_bytes(content)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {exc}",
        )

    # Регистрация через Lifecycle Manager
    try:
        config = await lifecycle.register(
            filename=filename,
            local_file_path=tmp_path,
            hostname=hostname,
            jvm_args=jvm_args,
            rate_limit=rate_limit,
        )
    except LifecycleError as exc:
        # Очистить временный файл при ошибке
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Опциональный немедленный запуск
    if start_immediately:
        try:
            config = await lifecycle.start(filename)
        except LifecycleError as exc:
            logger.warning(
                "Registered '%s' but failed to start: %s",
                filename, exc,
            )
            # Не бросаем ошибку — регистрация прошла успешно

    return _to_response(filename, config)


@router.delete(
    "/{mock_name}",
    response_model=MessageResponse,
    summary="Удалить заглушку",
)
async def delete_mock(
    mock_name: str,
    lifecycle: Annotated[LifecycleManager, Depends(get_lifecycle)],
) -> MessageResponse:
    """
    Удалить заглушку из системы.

    Если процесс запущен — будет остановлен.
    Файл на целевом хосте будет удалён.
    """
    try:
        await lifecycle.delete(mock_name)
    except LifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    return MessageResponse(message=f"Mock '{mock_name}' deleted")


@router.post(
    "/{mock_name}/start",
    response_model=MockResponse,
    summary="Запустить заглушку",
)
async def start_mock(
    mock_name: str,
    lifecycle: Annotated[LifecycleManager, Depends(get_lifecycle)],
) -> MockResponse:
    """
    Запустить зарегистрированную заглушку.

    Определяет свободный порт на целевом хосте
    и запускает Java-процесс через SSH (nohup).
    """
    try:
        config = await lifecycle.start(mock_name)
    except LifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return _to_response(mock_name, config)


@router.post(
    "/{mock_name}/stop",
    response_model=MockResponse,
    summary="Остановить заглушку",
)
async def stop_mock(
    mock_name: str,
    lifecycle: Annotated[LifecycleManager, Depends(get_lifecycle)],
) -> MockResponse:
    """
    Остановить запущенную заглушку.

    Отправляет SIGTERM, при неуспехе — SIGKILL через 5 секунд.
    """
    try:
        config = await lifecycle.stop(mock_name)
    except LifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return _to_response(mock_name, config)


@router.patch(
    "/{mock_name}/rate-limit",
    response_model=MockResponse,
    summary="Переключить Rate Limiter",
)
async def toggle_rate_limit(
    mock_name: str,
    body: MockRateLimitUpdate,
    mock_service: Annotated[MockService, Depends(get_mock_service)],
    rate_limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> MockResponse:
    """
    Включить или выключить Rate Limiter для заглушки.

    Изменение применяется немедленно — со следующего входящего
    запроса через прокси — без перезапуска процесса.
    """
    updated = await mock_service.update_field(
        mock_name,
        rate_limit_enabled=body.enabled,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mock '{mock_name}' not found",
        )

    # Сбросить счётчики при отключении
    if not body.enabled:
        await rate_limiter.reset(mock_name)

    return _to_response(mock_name, updated)


@router.patch(
    "/{mock_name}/jvm-args",
    response_model=MockResponse,
    summary="Обновить JVM-аргументы",
)
async def update_jvm_args(
    mock_name: str,
    body: JvmArgsUpdate,
    mock_service: Annotated[MockService, Depends(get_mock_service)],
) -> MockResponse:
    """
    Обновить JVM-аргументы заглушки.

    Изменения вступят в силу при следующем запуске.
    Если заглушка запущена — потребуется перезапуск.
    """
    updated = await mock_service.update_field(
        mock_name,
        jvm_args=body.jvm_args,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mock '{mock_name}' not found",
        )
    return _to_response(mock_name, updated)
