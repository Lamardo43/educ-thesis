from enum import Enum
from datetime import datetime
from pydantic import BaseModel


class MockStatus(str, Enum):
    REGISTERED = "REGISTERED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class MockRecord(BaseModel):
    filename: str
    hostname: str
    port: int | None = None
    pid: int | None = None
    status: MockStatus = MockStatus.REGISTERED
    jvm_args: str = ""
    # Шаблон аргумента порта. Используй {port} как placeholder.
    # Примеры:
    #   --server.port={port}   — Spring Boot (по умолчанию)
    #   {port}                 — позиционный аргумент (как в uuid-stub)
    #   --port={port}          — другие фреймворки
    #   -Dserver.port={port}   — через системное свойство JVM
    port_arg_template: str = "--server.port={port}"
    rate_limit: int = 500
    rate_limit_enabled: bool = False
    registered_at: datetime
    started_at: datetime | None = None


class CreateMockRequest(BaseModel):
    hostname: str
    jvm_args: str = ""
    port_arg_template: str = "--server.port={port}"
    rate_limit: int = 500
    start_immediately: bool = False


class UpdateRateLimitRequest(BaseModel):
    enabled: bool
    rate_limit: int | None = None
