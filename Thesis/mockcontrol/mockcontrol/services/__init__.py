"""
Пакет сервисного слоя MockControl.

Инкапсулирует все Redis-операции (GET/SET/DEL/SADD/SREM/Pipeline/WATCH),
предоставляя бизнес-логике (core/) и API-роутерам (api/) типизированный
интерфейс на основе Pydantic-моделей.

Каждый сервис отвечает за один тип сущности:
- MockService     — заглушки (mocks:*)
- HostService     — хосты (hosts:*)
- AccountService  — учётные записи SSH (accounts:*)
- SettingsService — глобальные настройки (settings:global)
"""

from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService

__all__ = [
    "AccountService",
    "HostService",
    "MockService",
    "SettingsService",
]
