"""SSR HTML-маршруты веб-интерфейса MockControl."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from dependencies import RedisClientDep, SettingsServiceDep
from models.host import HostStatus

MOCKS_REGISTRY_KEY = "mocks:registry"

templates = Jinja2Templates(directory="web/templates")
router = APIRouter(tags=["web"])


def _mock_key(filename: str) -> str:
    return f"mocks:{filename}"


def _parse_port(value: str | None) -> int | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _to_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() == "true"


async def _get_redis_connected(redis: RedisClientDep) -> bool:
    try:
        await redis.ping()
        return True
    except Exception:
        return False


@router.get("/", include_in_schema=False)
async def dashboard_page(
    request: Request,
    redis: RedisClientDep,
    settings_service: SettingsServiceDep,
):
    """Dashboard: сводка по заглушкам и хостам."""
    names = await redis.smembers(MOCKS_REGISTRY_KEY)
    ordered_names = sorted(names)

    mocks: list[dict[str, object]] = []
    if ordered_names:
        async with redis.pipeline(transaction=False) as pipe:
            for name in ordered_names:
                pipe.hgetall(_mock_key(name))
            rows = await pipe.execute()

        for name, row in zip(ordered_names, rows, strict=True):
            if not row:
                continue
            mocks.append(
                {
                    "filename": name,
                    "hostname": row.get("hostname", ""),
                    "status": row.get("status", "UNKNOWN"),
                    "port": _parse_port(row.get("port")),
                    "rate_limit_enabled": _to_bool(row.get("rate_limit_enabled")),
                }
            )

    hosts = await settings_service.list_hosts()

    total_mocks = len(mocks)
    running_mocks = sum(1 for mock in mocks if mock["status"] == "RUNNING")
    error_mocks = sum(1 for mock in mocks if mock["status"] == "ERROR")
    available_hosts = sum(1 for host in hosts if host.status == HostStatus.AVAILABLE)

    redis_connected = await _get_redis_connected(redis)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "mocks": mocks,
            "hosts": hosts,
            "total_mocks": total_mocks,
            "running_mocks": running_mocks,
            "error_mocks": error_mocks,
            "available_hosts": available_hosts,
            # Имена, которые сейчас использует шаблон dashboard.html.
            "mocks_total": total_mocks,
            "mocks_running": running_mocks,
            "mocks_errors": error_mocks,
            "hosts_available": available_hosts,
            "redis_connected": redis_connected,
            "active_page": "dashboard",
        },
    )


@router.get("/settings", include_in_schema=False)
async def settings_page(
    request: Request,
    redis: RedisClientDep,
    settings_service: SettingsServiceDep,
):
    """Страница настроек: хосты и учётные записи."""
    hosts = await settings_service.list_hosts()
    accounts = await settings_service.list_accounts()
    redis_connected = await _get_redis_connected(redis)

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "hosts": hosts,
            "accounts": accounts,
            "redis_connected": redis_connected,
            "active_page": "settings",
        },
    )


@router.get("/logs", include_in_schema=False)
async def logs_page(
    request: Request,
    redis: RedisClientDep,
):
    """Страница журналов: список имён заглушек."""
    names = sorted(await redis.smembers(MOCKS_REGISTRY_KEY))
    redis_connected = await _get_redis_connected(redis)

    # Шаблон logs.html ожидает элементы с полем filename.
    mocks = [{"filename": name} for name in names]

    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "mocks": mocks,
            "redis_connected": redis_connected,
            "active_page": "logs",
        },
    )
