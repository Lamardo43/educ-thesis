"""
Модели данных для учётных записей ОС Linux (SSH/SCP).

Включает:
- AccountConfig         — внутренняя модель, сериализуемая в Redis
- AccountCreateRequest  — входные данные при создании
- AccountUpdateRequest  — частичное обновление
- AccountResponse       — ответ API (пароль замаскирован)
- AccountListResponse   — обёртка для списка

Пароли шифруются перед записью в Redis (Fernet AES-128) и
никогда не возвращаются в ответах API.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AccountConfig(BaseModel):
    """
    Учётная запись SSH, хранимая в Redis.

    Ключ Redis: ``accounts:{uuid}``
    Тип: String (JSON-сериализация данной модели).

    Поле password_enc содержит Base64-кодированный зашифрованный блок
    (Fernet). Расшифровка выполняется CryptoService только в момент
    установки SSH-соединения — в оперативной памяти процесса.
    """

    username: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Имя пользователя ОС Linux",
    )
    password_enc: str = Field(
        ...,
        min_length=1,
        description="Зашифрованный пароль (Fernet AES-128, Base64)",
    )
    description: str = Field(
        "",
        max_length=500,
        description="Описание назначения учётной записи",
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Дата создания записи (UTC)",
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Проверить допустимость имени пользователя Linux."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Username must not be empty")
        # Linux usernames: lowercase, digits, hyphens, underscores
        import re
        if not re.match(r"^[a-z_][a-z0-9_\-]{0,63}$", stripped):
            raise ValueError(
                "Invalid Linux username. "
                "Use lowercase letters, digits, hyphens, underscores. "
                "Must start with a letter or underscore."
            )
        return stripped


class AccountCreateRequest(BaseModel):
    """Запрос на создание учётной записи."""

    username: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Имя пользователя ОС Linux",
    )
    password: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Пароль в открытом виде (шифруется перед записью в Redis)",
    )
    description: str = Field(
        "",
        max_length=500,
        description="Описание назначения учётной записи",
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Username must not be empty")
        import re
        if not re.match(r"^[a-z_][a-z0-9_\-]{0,63}$", stripped):
            raise ValueError(
                "Invalid Linux username. "
                "Use lowercase letters, digits, hyphens, underscores."
            )
        return stripped


class AccountUpdateRequest(BaseModel):
    """
    Запрос на частичное обновление учётной записи.

    Если передан пароль — он будет зашифрован и заменит текущий.
    """

    username: Optional[str] = Field(
        None, min_length=1, max_length=64,
    )
    password: Optional[str] = Field(
        None, min_length=1, max_length=256,
        description="Новый пароль (если передан — шифруется)",
    )
    description: Optional[str] = Field(
        None, max_length=500,
    )


class AccountResponse(BaseModel):
    """
    Ответ API с информацией об учётной записи.

    Пароль всегда замаскирован — ни при каких обстоятельствах
    не возвращается в открытом или зашифрованном виде.
    """

    uuid: str = Field(..., description="Уникальный идентификатор записи")
    username: str = Field(..., description="Имя пользователя ОС Linux")
    password_masked: str = Field(
        default="••••••••",
        description="Замаскированный пароль (не раскрывается)",
    )
    description: str
    created_at: datetime

    @classmethod
    def from_config(cls, account_uuid: str, config: AccountConfig) -> "AccountResponse":
        """Фабричный метод: конфигурация → ответ API (пароль маскируется)."""
        return cls(
            uuid=account_uuid,
            username=config.username,
            password_masked="••••••••",
            description=config.description,
            created_at=config.created_at,
        )


class AccountListResponse(BaseModel):
    """Список учётных записей с общим счётчиком."""

    total: int
    accounts: list[AccountResponse] = Field(default_factory=list)
