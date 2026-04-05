"""REST API учётных записей SSH: /api/v1/accounts (ТЗ, раздел «REST API» → «Учётные записи»)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from core.exceptions import AccountInUseError, AccountNotFoundError
from dependencies import SettingsServiceDep
from models.account import AccountCreate, AccountResponse, AccountUpdate
from services.settings_service import SettingsService

router = APIRouter(tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts(
    svc: SettingsServiceDep,
) -> list[AccountResponse]:
    """Список учётных записей без паролей."""
    return await svc.list_accounts()


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    data: AccountCreate,
    svc: SettingsServiceDep,
) -> AccountResponse:
    """Создать учётную запись (пароль шифруется в Redis)."""
    return await svc.create_account(data)


@router.put("/{uuid}", response_model=AccountResponse)
async def update_account(
    uuid: str,
    data: AccountUpdate,
    svc: SettingsServiceDep,
) -> AccountResponse:
    """Частичное обновление учётной записи."""
    try:
        return await svc.update_account(uuid, data)
    except AccountNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    uuid: str,
    svc: SettingsServiceDep,
) -> None:
    """Удалить учётную запись; если используется хостами — 409 и перечень hostname."""
    try:
        await svc.delete_account(uuid)
    except AccountNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AccountInUseError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "hosts": exc.hosts},
        ) from exc
