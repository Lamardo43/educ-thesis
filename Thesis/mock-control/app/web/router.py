from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.redis_client import get_redis
from app.models.mock import MockStatus
from app.models.host import HostStatus
from app.repositories.account_repo import list_accounts
from app.repositories.host_repo import list_hosts
from app.repositories.mock_repo import list_mocks
from app.repositories.settings_repo import get_settings

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory="templates")

RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, r: RedisDep):
    mocks = await list_mocks(r)
    hosts = await list_hosts(r)
    total = len(mocks)
    running = sum(1 for m in mocks if m.status == MockStatus.RUNNING)
    errors = sum(1 for m in mocks if m.status == MockStatus.ERROR)
    available_hosts = sum(1 for h in hosts if h.status == HostStatus.AVAILABLE)

    # Check Redis connectivity for the header indicator
    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "mocks": mocks,
        "hosts": hosts,
        "stats": {
            "total": total,
            "running": running,
            "errors": errors,
            "available_hosts": available_hosts,
        },
        "redis_ok": redis_ok,
        "MockStatus": MockStatus,
        "app_host": request.base_url.hostname,
        "app_port": request.base_url.port or 8000,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, r: RedisDep):
    hosts = await list_hosts(r)
    accounts = await list_accounts(r)
    global_settings = await get_settings(r)

    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "hosts": hosts,
        "accounts": accounts,
        "settings": global_settings,
        "redis_ok": redis_ok,
        "HostStatus": HostStatus,
    })


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, r: RedisDep):
    mocks = await list_mocks(r)

    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return templates.TemplateResponse("logs.html", {
        "request": request,
        "mocks": mocks,
        "redis_ok": redis_ok,
    })
