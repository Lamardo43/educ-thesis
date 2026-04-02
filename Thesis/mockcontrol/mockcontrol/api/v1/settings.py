"""
REST API для управления глобальными настройками системы.

Эндпоинты:
    GET  /api/v1/settings     — получить текущие настройки
    PUT  /api/v1/settings     — обновить настройки
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from mockcontrol.dependencies import get_settings_service
from mockcontrol.models.settings import GlobalSettings
from mockcontrol.services.settings_service import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "",
    response_model=GlobalSettings,
    summary="Получить глобальные настройки",
)
async def get_settings(
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> GlobalSettings:
    """Получить текущие значения глобальных настроек системы."""
    return await settings_service.get()


@router.put(
    "",
    response_model=GlobalSettings,
    summary="Обновить глобальные настройки",
)
async def update_settings(
    body: GlobalSettings,
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> GlobalSettings:
    """
    Обновить глобальные настройки системы.

    Изменения применяются немедленно:
    - rate_limit_window_size влияет на следующий запрос через прокси
    - host_check_interval_sec влияет на следующий цикл проверки хостов
    - proxy_timeout_sec влияет на следующий проксированный запрос
    """
    await settings_service.set(body)
    return body
