"""Pydantic-модели учётной записи SSH."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AccountCreate(BaseModel):
    """Входные данные создания учётной записи (пароль в открытом виде)."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1, description="Пароль до шифрования (Fernet)")
    description: str = ""

    @field_validator("username", "description", mode="before")
    @classmethod
    def strip_username_description(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("password")
    @classmethod
    def password_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Пароль не может быть пустым или состоять только из пробелов.")
        return v


class AccountConfig(BaseModel):
    """Данные учётной записи в Redis (Hash accounts:{uuid})."""

    username: str
    password_enc: str
    description: str = ""
    created_at: str


class AccountResponse(BaseModel):
    """Ответ API без секретов."""

    username: str
    uuid: str
    description: str = ""
    created_at: str


class AccountUpdate(BaseModel):
    """Частичное обновление учётной записи (PUT /api/v1/accounts/{uuid})."""

    username: Optional[str] = None
    password: Optional[str] = Field(default=None, description="Новый пароль в открытом виде; перешифровать")
    description: Optional[str] = None
