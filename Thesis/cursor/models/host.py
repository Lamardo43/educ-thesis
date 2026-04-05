"""Pydantic-модели хоста."""

from enum import Enum

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class HostStatus(str, Enum):
    """Статус хоста в Redis (поле status)."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class HostCreate(BaseModel):
    """Входные данные добавления хоста (вкладка «Хосты» в ТЗ)."""

    hostname: str = Field(min_length=1, description="Hostname или IP")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    account_uuid: str = Field(min_length=1)
    working_dir: str = Field(min_length=1)
    java_path: str = Field(min_length=1)
    mock_port_min: int = Field(ge=1, le=65535)
    mock_port_max: int = Field(ge=1, le=65535)
    description: str = ""

    @field_validator(
        "hostname",
        "account_uuid",
        "working_dir",
        "java_path",
        "description",
        mode="before",
    )
    @classmethod
    def strip_strings(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v


class HostConfig(BaseModel):
    """Полная конфигурация хоста (Hash hosts:{hostname})."""

    ssh_port: int = Field(default=22, ge=1, le=65535)
    account_uuid: str
    working_dir: str
    java_path: str
    mock_port_min: int = Field(ge=1, le=65535)
    mock_port_max: int = Field(ge=1, le=65535)
    description: str = ""
    status: HostStatus = HostStatus.UNKNOWN
    last_checked_at: str


class HostResponse(HostConfig):
    """Ответ API: конфигурация + идентификатор хоста."""

    hostname: str


class HostUpdate(BaseModel):
    """Частичное обновление хоста (PUT /api/v1/hosts/{hostname})."""

    ssh_port: Optional[int] = Field(default=None, ge=1, le=65535)
    account_uuid: Optional[str] = None
    working_dir: Optional[str] = None
    java_path: Optional[str] = None
    mock_port_min: Optional[int] = Field(default=None, ge=1, le=65535)
    mock_port_max: Optional[int] = Field(default=None, ge=1, le=65535)
    description: Optional[str] = None
