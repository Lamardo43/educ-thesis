"""
Веб-интерфейс MockControl — HTML-роуты (Jinja2 SSR).

Все страницы формируются на сервере и передаются браузеру
в готовом виде. JavaScript используется только для
интерактивных элементов (модалки, тумблеры, fetch-запросы).

Роуты:
    GET /              — редирект на /dashboard
    GET /dashboard     — главная панель управления
    GET /settings      — управление хостами и учётными записями
    GET /logs          — просмотр журналов процессов
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mockcontrol.dependencies import (
    get_account_service,
    get_host_service,
    get_mock_service,
    get_redis,
)
from mockcontrol.models.host import HostStatus
from mockcontrol.models.mock import MockStatus
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])

templates = Jinja2Templates(directory="mockcontrol/web/templates")


@router.get("/", include_in_schema=False)
async def root():
    """Редирект с корня на Dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(
    request: Request,
    mock_service: Annotated[MockService, Depends(get_mock_service)],
    host_service: Annotated[HostService, Depends(get_host_service)],
    redis=Depends(get_redis),
):
    """Главная панель управления заглушками."""
    # Данные для карточек и таблицы
    mock_configs = await mock_service.list_configs()
    host_configs = await host_service.list_configs()

    # Сводная статистика
    total_mocks = len(mock_configs)
    running = sum(1 for c in mock_configs.values() if c.status == MockStatus.RUNNING)
    errors = sum(1 for c in mock_configs.values() if c.status == MockStatus.ERROR)
    available_hosts = sum(1 for c in host_configs.values() if c.status == HostStatus.AVAILABLE)

    # Проверка Redis
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    # Список хостов для выпадающего списка в модалке
    host_list = sorted(host_configs.keys())

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "page": "dashboard",
        "redis_ok": redis_ok,
        "total_mocks": total_mocks,
        "running_mocks": running,
        "error_mocks": errors,
        "available_hosts": available_hosts,
        "mocks": mock_configs,
        "hosts": host_list,
    })


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(
    request: Request,
    host_service: Annotated[HostService, Depends(get_host_service)],
    account_service: Annotated[AccountService, Depends(get_account_service)],
    redis=Depends(get_redis),
):
    """Страница настроек: хосты и учётные записи."""
    host_configs = await host_service.list_configs()
    account_configs = await account_service.list_configs()

    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "page": "settings",
        "redis_ok": redis_ok,
        "hosts": host_configs,
        "accounts": account_configs,
    })


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs_page(
    request: Request,
    mock_service: Annotated[MockService, Depends(get_mock_service)],
    redis=Depends(get_redis),
):
    """Страница просмотра журналов процессов."""
    mock_configs = await mock_service.list_configs()

    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return templates.TemplateResponse("logs.html", {
        "request": request,
        "page": "logs",
        "redis_ok": redis_ok,
        "mocks": mock_configs,
    })
