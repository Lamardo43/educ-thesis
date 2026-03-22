import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.crypto import encrypt_password
from app.core.redis_client import get_redis
from app.models.account import AccountRecord, CreateAccountRequest
from app.repositories.account_repo import (
    delete_account,
    get_account,
    list_accounts,
    save_account,
)
from app.repositories.host_repo import list_hosts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


class AccountPublic(BaseModel):
    """Account record with masked password for API responses."""
    uuid: str
    username: str
    description: str
    created_at: datetime


@router.get("", response_model=list[AccountPublic])
async def list_all(r: RedisDep):
    accounts = await list_accounts(r)
    return [AccountPublic(uuid=a.uuid, username=a.username,
                          description=a.description, created_at=a.created_at)
            for a in accounts]


@router.post("", response_model=AccountPublic, status_code=201)
async def create(body: CreateAccountRequest, r: RedisDep):
    uid = str(_uuid.uuid4())
    account = AccountRecord(
        uuid=uid,
        username=body.username,
        password_enc=encrypt_password(body.password),
        description=body.description,
        created_at=datetime.now(timezone.utc),
    )
    await save_account(r, account)
    return AccountPublic(uuid=account.uuid, username=account.username,
                         description=account.description, created_at=account.created_at)


@router.put("/{uid}", response_model=AccountPublic)
async def update(uid: str, body: CreateAccountRequest, r: RedisDep):
    account = await get_account(r, uid)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account '{uid}' not found")
    account.username = body.username
    account.password_enc = encrypt_password(body.password)
    account.description = body.description
    await save_account(r, account)
    return AccountPublic(uuid=account.uuid, username=account.username,
                         description=account.description, created_at=account.created_at)


@router.delete("/{uid}", status_code=204)
async def remove(uid: str, r: RedisDep):
    account = await get_account(r, uid)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account '{uid}' not found")
    # Prevent deletion if any host references this account
    hosts = await list_hosts(r)
    using = [h.hostname for h in hosts if h.account_uuid == uid]
    if using:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete account '{uid}': used by host(s): {using}",
        )
    await delete_account(r, uid)
