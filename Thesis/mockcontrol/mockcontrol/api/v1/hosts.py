"""
REST API для управления реестром целевых хостов развёртывания.

Эндпоинты:
    GET    /api/v1/hosts                      — список всех хостов
    GET    /api/v1/hosts/{hostname}            — детали хоста
    POST   /api/v1/hosts                      — регистрация нового хоста
    PUT    /api/v1/hosts/{hostname}            — обновление хоста
    DELETE /api/v1/hosts/{hostname}            — удаление хоста
    POST   /api/v1/hosts/{hostname}/check      — принудительная проверка доступности
"""

import logging
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mockcontrol.core.host_checker import HostChecker
from mockcontrol.dependencies import (
    get_host_checker,
    get_host_service,
    get_mock_service,
)
from mockcontrol.models.host import HostConfig, HostCreateRequest, HostStatus
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hosts", tags=["hosts"])


# -------------------------------------------------------------------------
# Схемы ответов
# -------------------------------------------------------------------------


class HostResponse(BaseModel):
    """Ответ с информацией о хосте."""

    hostname: str
    ssh_port: int
    account_uuid: str
    working_dir: str
    java_path: str
    mock_port_min: int
    mock_port_max: int
    description: str
    status: HostStatus
    last_checked_at: Optional[datetime] = None


class HostListResponse(BaseModel):
    """Список хостов."""

    total: int
    hosts: list[HostResponse]


class MessageResponse(BaseModel):
    message: str


class HostUpdateRequest(BaseModel):
    """Запрос на обновление параметров хоста."""

    ssh_port: Optional[int] = Field(None, ge=1, le=65535)
    account_uuid: Optional[str] = None
    working_dir: Optional[str] = None
    java_path: Optional[str] = None
    mock_port_min: Optional[int] = Field(None, ge=1024, le=65535)
    mock_port_max: Optional[int] = Field(None, ge=1024, le=65535)
    description: Optional[str] = None


class HostCheckResponse(BaseModel):
    """Результат проверки доступности хоста."""

    hostname: str
    status: HostStatus
    checked_at: datetime


# -------------------------------------------------------------------------
# Вспомогательные функции
# -------------------------------------------------------------------------


def _to_response(hostname: str, config: HostConfig) -> HostResponse:
    return HostResponse(
        hostname=hostname,
        ssh_port=config.ssh_port,
        account_uuid=config.account_uuid,
        working_dir=config.working_dir,
        java_path=config.java_path,
        mock_port_min=config.mock_port_min,
        mock_port_max=config.mock_port_max,
        description=config.description,
        status=config.status,
        last_checked_at=config.last_checked_at,
    )


# -------------------------------------------------------------------------
# Эндпоинты
# -------------------------------------------------------------------------


@router.get(
    "",
    response_model=HostListResponse,
    summary="Получить список всех хостов",
)
async def list_hosts(
    host_service: Annotated[HostService, Depends(get_host_service)],
) -> HostListResponse:
    """Получить полный список зарегистрированных целевых хостов."""
    configs = await host_service.list_configs()
    hosts = [_to_response(h, cfg) for h, cfg in configs.items()]
    return HostListResponse(total=len(hosts), hosts=hosts)


@router.get(
    "/{hostname}",
    response_model=HostResponse,
    summary="Получить информацию о хосте",
)
async def get_host(
    hostname: str,
    host_service: Annotated[HostService, Depends(get_host_service)],
) -> HostResponse:
    """Получить детальную информацию о конкретном хосте."""
    config = await host_service.get(hostname)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Host '{hostname}' not found",
        )
    return _to_response(hostname, config)


@router.post(
    "",
    response_model=HostResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать новый хост",
)
async def register_host(
    body: HostCreateRequest,
    host_service: Annotated[HostService, Depends(get_host_service)],
    host_checker: Annotated[HostChecker, Depends(get_host_checker)],
) -> HostResponse:
    """
    Зарегистрировать новый целевой хост в реестре.

    После регистрации немедленно выполняется проверка
    доступности SSH-соединения.
    """
    # Валидация диапазона портов
    if body.mock_port_min > body.mock_port_max:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mock_port_min must be <= mock_port_max",
        )

    config = HostConfig(
        ssh_port=body.ssh_port,
        account_uuid=body.account_uuid,
        working_dir=body.working_dir,
        java_path=body.java_path,
        mock_port_min=body.mock_port_min,
        mock_port_max=body.mock_port_max,
        description=body.description,
        status=HostStatus.UNKNOWN,
    )

    registered = await host_service.register(body.hostname, config)
    if not registered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Host '{body.hostname}' already registered",
        )

    # Немедленная проверка доступности
    new_status = await host_checker.check_single(body.hostname)
    logger.info(
        "Registered host '%s', initial status: %s",
        body.hostname, new_status.value,
    )

    # Перечитать обновлённую запись
    updated = await host_service.get(body.hostname)
    return _to_response(body.hostname, updated)


@router.put(
    "/{hostname}",
    response_model=HostResponse,
    summary="Обновить параметры хоста",
)
async def update_host(
    hostname: str,
    body: HostUpdateRequest,
    host_service: Annotated[HostService, Depends(get_host_service)],
) -> HostResponse:
    """
    Обновить параметры существующего хоста.

    Передаются только изменяемые поля — остальные сохраняются.
    """
    # Собрать только переданные поля (не None)
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    # Валидация диапазона портов при частичном обновлении
    if "mock_port_min" in update_data or "mock_port_max" in update_data:
        current = await host_service.get(hostname)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Host '{hostname}' not found",
            )
        new_min = update_data.get("mock_port_min", current.mock_port_min)
        new_max = update_data.get("mock_port_max", current.mock_port_max)
        if new_min > new_max:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mock_port_min must be <= mock_port_max",
            )

    updated = await host_service.update_field(hostname, **update_data)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Host '{hostname}' not found",
        )
    return _to_response(hostname, updated)


@router.delete(
    "/{hostname}",
    response_model=MessageResponse,
    summary="Удалить хост из реестра",
)
async def delete_host(
    hostname: str,
    host_service: Annotated[HostService, Depends(get_host_service)],
    mock_service: Annotated[MockService, Depends(get_mock_service)],
) -> MessageResponse:
    """
    Удалить хост из реестра.

    Операция запрещена, если на хосте размещены заглушки.
    Сначала необходимо удалить или перенести все заглушки.
    """
    # Проверить, нет ли заглушек на этом хосте
    mock_configs = await mock_service.list_configs()
    mocks_on_host = [
        fn for fn, cfg in mock_configs.items()
        if cfg.hostname == hostname
    ]
    if mocks_on_host:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete host '{hostname}': "
                f"{len(mocks_on_host)} mock(s) deployed on it: "
                f"{', '.join(mocks_on_host)}"
            ),
        )

    config = await host_service.get(hostname)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Host '{hostname}' not found",
        )

    await host_service.delete(hostname)
    return MessageResponse(message=f"Host '{hostname}' deleted")


@router.post(
    "/{hostname}/check",
    response_model=HostCheckResponse,
    summary="Проверить доступность хоста",
)
async def check_host(
    hostname: str,
    host_service: Annotated[HostService, Depends(get_host_service)],
    host_checker: Annotated[HostChecker, Depends(get_host_checker)],
) -> HostCheckResponse:
    """
    Принудительно проверить доступность SSH-соединения к хосту.

    Результат немедленно записывается в Redis.
    """
    config = await host_service.get(hostname)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Host '{hostname}' not found",
        )

    new_status = await host_checker.check_single(hostname)
    return HostCheckResponse(
        hostname=hostname,
        status=new_status,
        checked_at=datetime.utcnow(),
    )
