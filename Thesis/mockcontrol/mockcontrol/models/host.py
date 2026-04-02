"""
Модели данных для целевых хостов развёртывания.

Включает:
- HostStatus          — перечисление статусов доступности
- HostConfig          — внутренняя модель, сериализуемая в Redis
- HostCreateRequest   — входные данные при регистрации
- HostUpdateRequest   — частичное обновление параметров
- HostResponse        — ответ API
- HostListResponse    — обёртка для списка
- HostCheckResponse   — результат проверки доступности
"""

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# Валидация hostname: IPv4, IPv6 или DNS-имя
_HOSTNAME_PATTERN = re.compile(
    r"^("
    r"((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)"  # IPv4
    r"|"
    r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"                        # DNS label
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*"                   # DNS dots
    r")$"
)

# Валидация Unix-пути
_UNIX_PATH_PATTERN = re.compile(r"^/[a-zA-Z0-9_./ \-]+$")


class HostStatus(str, Enum):
    """
    Статус доступности хоста.

    AVAILABLE   — SSH-соединение успешно установлено.
    UNAVAILABLE — попытка соединения завершилась ошибкой.
    UNKNOWN     — проверка ещё не выполнялась.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class HostConfig(BaseModel):
    """
    Конфигурация целевого хоста, хранимая в Redis.

    Ключ Redis: ``hosts:{hostname}``
    Тип: String (JSON-сериализация данной модели).
    """

    ssh_port: int = Field(
        22, ge=1, le=65535,
        description="Порт SSH-сервера на целевом хосте",
    )
    account_uuid: str = Field(
        ...,
        min_length=36,
        max_length=36,
        description="UUID учётной записи SSH из реестра accounts",
    )
    working_dir: str = Field(
        ...,
        min_length=2,
        max_length=512,
        description="Абсолютный путь к рабочей директории на хосте",
    )
    java_path: str = Field(
        "/usr/bin/java",
        min_length=2,
        max_length=512,
        description="Абсолютный путь к исполняемому файлу Java",
    )
    mock_port_min: int = Field(
        8100, ge=1024, le=65535,
        description="Нижняя граница диапазона портов для заглушек",
    )
    mock_port_max: int = Field(
        8200, ge=1024, le=65535,
        description="Верхняя граница диапазона портов для заглушек",
    )
    description: str = Field(
        "",
        max_length=500,
        description="Описание назначения хоста",
    )
    status: HostStatus = Field(
        HostStatus.UNKNOWN,
        description="Текущий статус доступности SSH",
    )
    last_checked_at: Optional[datetime] = Field(
        None,
        description="Дата и время последней проверки доступности (UTC)",
    )

    @field_validator("working_dir", "java_path")
    @classmethod
    def validate_unix_path(cls, v: str) -> str:
        """Проверить, что путь является абсолютным Unix-путём."""
        stripped = v.strip()
        if not stripped.startswith("/"):
            raise ValueError("Path must be absolute (start with /)")
        if not _UNIX_PATH_PATTERN.match(stripped):
            raise ValueError(
                "Path contains invalid characters. "
                "Allowed: letters, digits, _, ., -, /, space"
            )
        return stripped

    @model_validator(mode="after")
    def validate_port_range(self) -> "HostConfig":
        """Убедиться, что mock_port_min <= mock_port_max."""
        if self.mock_port_min > self.mock_port_max:
            raise ValueError(
                f"mock_port_min ({self.mock_port_min}) must be "
                f"<= mock_port_max ({self.mock_port_max})"
            )
        return self

    @property
    def port_range_size(self) -> int:
        """Количество портов в диапазоне."""
        return self.mock_port_max - self.mock_port_min + 1

    @property
    def is_available(self) -> bool:
        return self.status == HostStatus.AVAILABLE


class HostCreateRequest(BaseModel):
    """Запрос на регистрацию нового хоста."""

    hostname: str = Field(
        ..., min_length=1, max_length=253,
        description="IP-адрес или DNS-имя хоста",
    )
    ssh_port: int = Field(22, ge=1, le=65535)
    account_uuid: str = Field(..., min_length=36, max_length=36)
    working_dir: str = Field(..., min_length=2, max_length=512)
    java_path: str = Field("/usr/bin/java", min_length=2, max_length=512)
    mock_port_min: int = Field(8100, ge=1024, le=65535)
    mock_port_max: int = Field(8200, ge=1024, le=65535)
    description: str = Field("", max_length=500)

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        stripped = v.strip()
        if not _HOSTNAME_PATTERN.match(stripped):
            raise ValueError(
                "Invalid hostname. Use IPv4, IPv6 or a valid DNS name."
            )
        return stripped

    @field_validator("working_dir", "java_path")
    @classmethod
    def validate_unix_path(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped.startswith("/"):
            raise ValueError("Path must be absolute (start with /)")
        return stripped

    @model_validator(mode="after")
    def validate_port_range(self) -> "HostCreateRequest":
        if self.mock_port_min > self.mock_port_max:
            raise ValueError(
                f"mock_port_min ({self.mock_port_min}) must be "
                f"<= mock_port_max ({self.mock_port_max})"
            )
        return self


class HostUpdateRequest(BaseModel):
    """
    Запрос на частичное обновление параметров хоста.

    Передаются только изменяемые поля — остальные сохраняются.
    """

    ssh_port: Optional[int] = Field(None, ge=1, le=65535)
    account_uuid: Optional[str] = Field(None, min_length=36, max_length=36)
    working_dir: Optional[str] = Field(None, min_length=2, max_length=512)
    java_path: Optional[str] = Field(None, min_length=2, max_length=512)
    mock_port_min: Optional[int] = Field(None, ge=1024, le=65535)
    mock_port_max: Optional[int] = Field(None, ge=1024, le=65535)
    description: Optional[str] = Field(None, max_length=500)


class HostResponse(BaseModel):
    """Ответ API с информацией о хосте."""

    hostname: str
    ssh_port: int
    account_uuid: str
    working_dir: str
    java_path: str
    mock_port_min: int
    mock_port_max: int
    description: str
    status: HostStatus
    last_checked_at: Optional[datetime] = None
    port_range_size: int = Field(..., description="Кол-во портов в диапазоне")

    @classmethod
    def from_config(cls, hostname: str, config: HostConfig) -> "HostResponse":
        return cls(
            hostname=hostname,
            ssh_port=config.ssh_port,
            account_uuid=config.account_uuid,
            working_dir=config.working_dir,
            java_path=config.java_path,
            mock_port_min=config.mock_port_min,
            mock_port_max=config.mock_port_max,
            description=config.description,
            status=config.status,
            last_checked_at=config.last_checked_at,
            port_range_size=config.port_range_size,
        )


class HostListResponse(BaseModel):
    """Список хостов с общим счётчиком."""

    total: int
    hosts: list[HostResponse] = Field(default_factory=list)


class HostCheckResponse(BaseModel):
    """Результат проверки доступности хоста."""

    hostname: str
    status: HostStatus
    checked_at: datetime
