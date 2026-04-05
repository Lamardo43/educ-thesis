"""REST API хостов: /api/v1/hosts (ТЗ, раздел «REST API» → «Хосты»)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status

from core.exceptions import (
    AccountNotFoundError,
    HostAlreadyExistsError,
    HostHasMocksError,
    HostNotFoundError,
)
from dependencies import SettingsServiceDep
from models.host import HostCreate, HostResponse, HostUpdate

router = APIRouter(tags=["hosts"])


@router.get("", response_model=list[HostResponse])
async def list_hosts(
    svc: SettingsServiceDep,
) -> list[HostResponse]:
    """Список хостов: реестр + pipeline HGETALL (см. SettingsService.list_hosts)."""
    return await svc.list_hosts()


@router.post("", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(
    data: HostCreate,
    svc: SettingsServiceDep,
) -> HostResponse:
    """Добавить хост (HostCreate)."""
    try:
        return await svc.create_host(data)
    except HostAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AccountNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{hostname}", response_model=HostResponse)
async def update_host(
    hostname: str,
    data: HostUpdate,
    svc: SettingsServiceDep,
) -> HostResponse:
    """Обновить поля хоста (HostUpdate)."""
    try:
        return await svc.update_host(hostname, data)
    except HostNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AccountNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{hostname}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(
    hostname: str,
    svc: SettingsServiceDep,
) -> None:
    """Удалить хост; при привязанных заглушках — 409 и перечень имён файлов."""
    try:
        await svc.delete_host(hostname)
    except HostNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except HostHasMocksError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "mocks": exc.mocks},
        ) from exc
