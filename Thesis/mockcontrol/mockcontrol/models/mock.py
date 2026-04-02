"""
Модели данных для имитационных сервисов (заглушек).

Включает:
- MockStatus          — перечисление статусов жизненного цикла
- MockConfig          — внутренняя модель, сериализуемая в Redis
- MockCreateRequest   — входные данные при регистрации (Form-поля)
- MockResponse        — ответ API с информацией о заглушке
- MockListResponse    — обёртка для списка заглушек
- MockRateLimitUpdate — тело PATCH-запроса переключения Rate Limiter
- JvmArgsUpdate       — тело PATCH-запроса обновления JVM-аргументов
"""

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MockStatus(str, Enum):
    """
    Статус жизненного цикла заглушки.

    REGISTERED — артефакт загружен на хост, процесс не запускался.
    RUNNING    — Java-процесс активен, порт назначен.
    STOPPED    — процесс был остановлен штатно.
    ERROR      — ошибка запуска или потеря связи с хостом.
    """

    REGISTERED = "REGISTERED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


# Паттерн для базовой валидации JVM-аргументов:
# допускаются флаги вида -Xms128m, -Xmx256m, -Dfoo=bar, --param=val
_JVM_ARG_PATTERN = re.compile(
    r"^(-[a-zA-Z][a-zA-Z0-9_.]*(?:[:=][^\s]*)?\s*)*$"
)


def _validate_jvm(v: str) -> str:
    """Общая валидация JVM-аргументов (переиспользуется в нескольких моделях)."""
    stripped = v.strip()
    if stripped and not _JVM_ARG_PATTERN.match(stripped):
        raise ValueError(
            "Invalid JVM arguments format. "
            "Expected: -Xms128m -Xmx256m -Dproperty=value"
        )
    return stripped


class MockConfig(BaseModel):
    """
    Конфигурация заглушки, хранимая в Redis.

    Ключ Redis: ``mocks:{filename}``
    Тип: String (JSON-сериализация данной модели).

    Поля port, pid и started_at заполняются только после успешного
    запуска процесса и сбрасываются в None при остановке или удалении.
    """

    hostname: str = Field(
        ...,
        min_length=1,
        max_length=253,
        description="Целевой хост развёртывания (hostname или IP-адрес)",
    )
    port: Optional[int] = Field(
        None,
        ge=1024,
        le=65535,
        description="TCP-порт запущенного Java-процесса",
    )
    pid: Optional[int] = Field(
        None,
        ge=1,
        description="PID процесса на целевом хосте",
    )
    status: MockStatus = Field(
        MockStatus.REGISTERED,
        description="Текущий статус жизненного цикла",
    )
    jvm_args: str = Field(
        "",
        max_length=2000,
        description="Аргументы JVM: -Xms128m -Xmx256m -Dprop=val",
    )
    rate_limit: int = Field(
        0,
        ge=0,
        le=1_000_000,
        description="Макс. запросов в секунду (0 = без ограничений)",
    )
    rate_limit_enabled: bool = Field(
        False,
        description="Флаг активности Rate Limiter",
    )
    registered_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Дата и время регистрации (UTC)",
    )
    started_at: Optional[datetime] = Field(
        None,
        description="Дата и время последнего запуска (UTC)",
    )

    @field_validator("jvm_args")
    @classmethod
    def validate_jvm_args(cls, v: str) -> str:
        return _validate_jvm(v)

    @property
    def is_running(self) -> bool:
        """Запущена ли заглушка."""
        return self.status == MockStatus.RUNNING

    @staticmethod
    def stopped_fields() -> dict:
        """Поля для сброса при остановке процесса."""
        return {"port": None, "pid": None, "started_at": None}


class MockCreateRequest(BaseModel):
    """
    Входные данные для регистрации новой заглушки.

    Используются как Form-поля при multipart/form-data загрузке.
    Файл артефакта (.jar/.war) передаётся отдельно через UploadFile.
    """

    hostname: str = Field(
        ..., min_length=1, max_length=253,
        description="Целевой хост из реестра",
    )
    jvm_args: str = Field(
        "", max_length=2000,
        description="Аргументы JVM",
    )
    rate_limit: int = Field(
        0, ge=0, le=1_000_000,
        description="Макс. RPS (0 = без ограничений)",
    )
    start_immediately: bool = Field(
        False,
        description="Запустить процесс сразу после регистрации",
    )


class MockResponse(BaseModel):
    """
    Ответ API с полной информацией о заглушке.

    Используется во всех эндпоинтах, возвращающих данные заглушки.
    """

    filename: str = Field(..., description="Имя файла артефакта (уникальный ID)")
    hostname: str = Field(..., description="Целевой хост")
    port: Optional[int] = Field(None, description="Назначенный TCP-порт")
    pid: Optional[int] = Field(None, description="PID процесса")
    status: MockStatus = Field(..., description="Текущий статус")
    jvm_args: str = Field(..., description="JVM-аргументы")
    rate_limit: int = Field(..., description="Порог Rate Limiter")
    rate_limit_enabled: bool = Field(..., description="Rate Limiter активен")
    registered_at: datetime = Field(..., description="Дата регистрации")
    started_at: Optional[datetime] = Field(None, description="Дата запуска")
    proxy_url: Optional[str] = Field(
        None,
        description="URL для обращения через прокси: /{filename}/...",
    )

    @classmethod
    def from_config(cls, filename: str, config: MockConfig) -> "MockResponse":
        """Фабричный метод: собрать ответ из имени файла и конфигурации Redis."""
        proxy_url = f"/{filename}" if config.is_running else None
        return cls(
            filename=filename,
            hostname=config.hostname,
            port=config.port,
            pid=config.pid,
            status=config.status,
            jvm_args=config.jvm_args,
            rate_limit=config.rate_limit,
            rate_limit_enabled=config.rate_limit_enabled,
            registered_at=config.registered_at,
            started_at=config.started_at,
            proxy_url=proxy_url,
        )


class MockListResponse(BaseModel):
    """Список заглушек с общим счётчиком."""

    total: int = Field(..., description="Общее число заглушек")
    mocks: list[MockResponse] = Field(default_factory=list)


class MockRateLimitUpdate(BaseModel):
    """Тело PATCH-запроса для переключения Rate Limiter."""

    enabled: bool = Field(
        ..., description="Включить (true) или выключить (false)",
    )


class JvmArgsUpdate(BaseModel):
    """Тело PATCH-запроса для обновления JVM-аргументов."""

    jvm_args: str = Field(
        ..., max_length=2000,
        description="Новые аргументы JVM (вступают в силу при следующем запуске)",
    )

    @field_validator("jvm_args")
    @classmethod
    def validate_jvm_args(cls, v: str) -> str:
        return _validate_jvm(v)
