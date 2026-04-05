"""REST API глобальных настроек: /api/v1/settings (Hash settings:global, ТЗ)."""

from __future__ import annotations

from fastapi import APIRouter

from dependencies import SettingsServiceDep
from models.settings import GlobalSettings, GlobalSettingsUpdate

router = APIRouter(tags=["settings"])


@router.get("", response_model=GlobalSettings)
async def get_global_settings(
    svc: SettingsServiceDep,
) -> GlobalSettings:
    """Глобальные настройки (HGETALL settings:global или дефолты ТЗ)."""
    return await svc.get_settings()


@router.patch("", response_model=GlobalSettings)
async def patch_global_settings(
    payload: GlobalSettingsUpdate,
    svc: SettingsServiceDep,
) -> GlobalSettings:
    """Частичное обновление полей settings:global (HSET)."""
    return await svc.update_settings(payload)
