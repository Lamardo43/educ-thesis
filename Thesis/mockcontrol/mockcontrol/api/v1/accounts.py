"""
REST API для управления учётными записями ОС Linux (SSH/SCP).

Пароли шифруются перед записью в Redis и никогда не возвращаются
в ответах API — вместо них показывается замаскированная строка.

Эндпоинты:
    GET    /api/v1/accounts                — список всех учётных записей
    GET    /api/v1/accounts/{uuid}          — детали учётной записи
    POST   /api/v1/accounts                — создание
    PUT    /api/v1/accounts/{uuid}          — обновление
    DELETE /api/v1/accounts/{uuid}          — удаление
"""

import logging
import uuid as uuid_mod
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mockcontrol.core.crypto import CryptoService
from mockcontrol.dependencies import (
    get_account_service,
    get_crypto,
    get_host_service,
)
from mockcontrol.models.account import AccountConfig, AccountCreateRequest
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


# -------------------------------------------------------------------------
# Схемы ответов
# -------------------------------------------------------------------------


class AccountResponse(BaseModel):
    """Ответ с информацией об учётной записи (пароль замаскирован)."""

    uuid: str
    username: str
    password_masked: str = Field(
        default="••••••••",
        description="Замаскированный пароль (никогда не раскрывается)",
    )
    description: str
    created_at: datetime


class AccountListResponse(BaseModel):
    """Список учётных записей."""

    total: int
    accounts: list[AccountResponse]


class MessageResponse(BaseModel):
    message: str


class AccountUpdateRequest(BaseModel):
    """Запрос на обновление учётной записи."""

    username: Optional[str] = None
    password: Optional[str] = Field(
        None,
        description="Новый пароль (если передан — будет зашифрован)",
    )
    description: Optional[str] = None


# -------------------------------------------------------------------------
# Вспомогательные функции
# -------------------------------------------------------------------------


def _to_response(account_uuid: str, config: AccountConfig) -> AccountResponse:
    return AccountResponse(
        uuid=account_uuid,
        username=config.username,
        password_masked="••••••••",
        description=config.description,
        created_at=config.created_at,
    )


# -------------------------------------------------------------------------
# Эндпоинты
# -------------------------------------------------------------------------


@router.get(
    "",
    response_model=AccountListResponse,
    summary="Получить список учётных записей",
)
async def list_accounts(
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> AccountListResponse:
    """Получить полный список зарегистрированных учётных записей SSH."""
    configs = await account_service.list_configs()
    accounts = [_to_response(u, cfg) for u, cfg in configs.items()]
    return AccountListResponse(total=len(accounts), accounts=accounts)


@router.get(
    "/{account_uuid}",
    response_model=AccountResponse,
    summary="Получить учётную запись",
)
async def get_account(
    account_uuid: str,
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> AccountResponse:
    """Получить информацию об учётной записи (пароль замаскирован)."""
    config = await account_service.get(account_uuid)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{account_uuid}' not found",
        )
    return _to_response(account_uuid, config)


@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать учётную запись",
)
async def create_account(
    body: AccountCreateRequest,
    account_service: Annotated[AccountService, Depends(get_account_service)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
) -> AccountResponse:
    """
    Создать новую учётную запись для SSH/SCP-операций.

    Пароль шифруется алгоритмом AES-128 (Fernet) перед
    записью в Redis. Ключ шифрования хранится на файловой
    системе сервера (вне Redis).
    """
    account_uuid = str(uuid_mod.uuid4())

    config = AccountConfig(
        username=body.username,
        password_enc=crypto.encrypt(body.password),
        description=body.description,
        created_at=datetime.utcnow(),
    )

    await account_service.register(account_uuid, config)
    logger.info("Created account '%s' (user: %s)", account_uuid, body.username)

    return _to_response(account_uuid, config)


@router.put(
    "/{account_uuid}",
    response_model=AccountResponse,
    summary="Обновить учётную запись",
)
async def update_account(
    account_uuid: str,
    body: AccountUpdateRequest,
    account_service: Annotated[AccountService, Depends(get_account_service)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
) -> AccountResponse:
    """
    Обновить параметры учётной записи.

    Если передан новый пароль — он будет зашифрован и заменит старый.
    """
    current = await account_service.get(account_uuid)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{account_uuid}' not found",
        )

    update_data: dict = {}

    if body.username is not None:
        update_data["username"] = body.username
    if body.description is not None:
        update_data["description"] = body.description
    if body.password is not None:
        update_data["password_enc"] = crypto.encrypt(body.password)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    updated = current.model_copy(update=update_data)
    await account_service.set(account_uuid, updated)

    logger.info("Updated account '%s'", account_uuid)
    return _to_response(account_uuid, updated)


@router.delete(
    "/{account_uuid}",
    response_model=MessageResponse,
    summary="Удалить учётную запись",
)
async def delete_account(
    account_uuid: str,
    account_service: Annotated[AccountService, Depends(get_account_service)],
    host_service: Annotated[HostService, Depends(get_host_service)],
) -> MessageResponse:
    """
    Удалить учётную запись.

    Операция запрещена, если учётная запись используется
    хотя бы одним хостом из реестра.
    """
    # Проверить использование
    host_configs = await host_service.list_configs()
    hosts_using = [
        h for h, cfg in host_configs.items()
        if cfg.account_uuid == account_uuid
    ]
    if hosts_using:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete account '{account_uuid}': "
                f"used by host(s): {', '.join(hosts_using)}"
            ),
        )

    config = await account_service.get(account_uuid)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{account_uuid}' not found",
        )

    await account_service.delete(account_uuid)
    logger.info("Deleted account '%s'", account_uuid)
    return MessageResponse(message=f"Account '{account_uuid}' deleted")
