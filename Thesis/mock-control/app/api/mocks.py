import logging
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core.redis_client import get_redis
from app.models.mock import MockRecord, UpdateRateLimitRequest
from app.repositories.mock_repo import get_mock, list_mocks, save_mock
from app.repositories.settings_repo import get_settings
from app.services.lifecycle import (
    delete_mock_service,
    get_mock_logs,
    register_mock,
    start_mock,
    stop_mock,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/mocks", tags=["mocks"])

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]

_ALLOWED_EXTENSIONS = {".jar", ".war"}


def _validate_filename(filename: str) -> None:
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only .jar and .war files are allowed, got '{ext}'")


# ── List ──────────────────────────────────────────────────────────────────

@router.get("", response_model=list[MockRecord])
async def list_all_mocks(r: RedisDep):
    return await list_mocks(r)


# ── Register ──────────────────────────────────────────────────────────────

@router.post("", response_model=MockRecord, status_code=201)
async def register(
    r: RedisDep,
    file: UploadFile = File(...),
    hostname: str = Form(...),
    jvm_args: str = Form(""),
    port_arg_template: str = Form("--server.port={port}"),
    rate_limit: int = Form(500),
    start_immediately: bool = Form(False),
):
    filename = file.filename or ""
    _validate_filename(filename)
    file_bytes = await file.read()
    try:
        mock = await register_mock(
            r=r,
            filename=filename,
            file_bytes=file_bytes,
            hostname=hostname,
            jvm_args=jvm_args,
            port_arg_template=port_arg_template,
            rate_limit=rate_limit,
            start_immediately=start_immediately,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to register mock")
        raise HTTPException(status_code=500, detail=str(exc))
    return mock


# ── Get single ────────────────────────────────────────────────────────────

@router.get("/{mock_name}", response_model=MockRecord)
async def get_one(mock_name: str, r: RedisDep):
    mock = await get_mock(r, mock_name)
    if mock is None:
        raise HTTPException(status_code=404, detail=f"Mock '{mock_name}' not found")
    return mock


# ── Start ─────────────────────────────────────────────────────────────────

@router.post("/{mock_name}/start", response_model=MockRecord)
async def start(mock_name: str, r: RedisDep):
    try:
        return await start_mock(r, mock_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to start mock '%s'", mock_name)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Stop ──────────────────────────────────────────────────────────────────

@router.post("/{mock_name}/stop", response_model=MockRecord)
async def stop(mock_name: str, r: RedisDep):
    try:
        return await stop_mock(r, mock_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to stop mock '%s'", mock_name)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Delete ────────────────────────────────────────────────────────────────

@router.delete("/{mock_name}", status_code=204)
async def delete(mock_name: str, r: RedisDep):
    mock = await get_mock(r, mock_name)
    if mock is None:
        raise HTTPException(status_code=404, detail=f"Mock '{mock_name}' not found")
    try:
        await delete_mock_service(r, mock_name)
    except Exception as exc:
        logger.exception("Failed to delete mock '%s'", mock_name)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Rate Limit toggle ─────────────────────────────────────────────────────

@router.patch("/{mock_name}/rate-limit", response_model=MockRecord)
async def update_rate_limit(mock_name: str, body: UpdateRateLimitRequest, r: RedisDep):
    mock = await get_mock(r, mock_name)
    if mock is None:
        raise HTTPException(status_code=404, detail=f"Mock '{mock_name}' not found")
    mock.rate_limit_enabled = body.enabled
    if body.rate_limit is not None:
        mock.rate_limit = body.rate_limit
    await save_mock(r, mock)
    return mock


# ── Logs ──────────────────────────────────────────────────────────────────

@router.get("/{mock_name}/logs", response_class=PlainTextResponse)
async def get_logs(mock_name: str, r: RedisDep, lines: int = 200):
    settings = await get_settings(r)
    n = min(lines, settings.log_retention_lines)
    try:
        log_text = await get_mock_logs(r, mock_name, lines=n)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to fetch logs for '%s'", mock_name)
        raise HTTPException(status_code=500, detail=str(exc))
    return log_text
