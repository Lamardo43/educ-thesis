"""Pydantic-модели заглушки (mock)."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MockStatus(str, Enum):
    """Статус заглушки в Redis (поле status)."""

    REGISTERED = "REGISTERED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class MockCreate(BaseModel):
    """Входные данные регистрации; файл передаётся отдельно (UploadFile)."""

    hostname: str
    jvm_args: str = ""
    rate_limit: int = Field(
        default=0,
        ge=0,
        description="Макс. запросов в секунду; 0 — без ограничений",
    )
    auto_start: bool = False


class MockConfig(BaseModel):
    """Полная конфигурация заглушки (Hash mocks:{filename})."""

    hostname: str
    port: Optional[int] = None
    pid: Optional[int] = None
    status: MockStatus
    jvm_args: str = ""
    rate_limit: int = Field(default=0, ge=0)
    rate_limit_enabled: bool = False
    registered_at: str
    started_at: Optional[str] = None
    artifact_path: str


class MockResponse(MockConfig):
    """Ответ API: конфигурация + имя файла артефакта."""

    filename: str


class MockRateLimitUpdate(BaseModel):
    """PATCH /mocks/{filename}/rate-limit — переключение Rate Limiter."""

    enabled: bool


class MockConfigUpdate(BaseModel):
    """PATCH /mocks/{filename}/config — обновление JVM и лимита."""

    jvm_args: Optional[str] = None
    rate_limit: Optional[int] = Field(default=None, ge=0)
