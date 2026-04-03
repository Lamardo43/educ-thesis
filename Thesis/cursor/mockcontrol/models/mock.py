from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MockStatus(str, Enum):
    REGISTERED = "REGISTERED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class MockCreate(BaseModel):
    """Registration input; artifact file is sent separately as UploadFile."""

    hostname: str
    jvm_args: str = ""
    rate_limit: int = Field(ge=0, description="0 means no limit")
    auto_start: bool = False


class MockConfig(BaseModel):
    """Full mock record as stored in Redis hash mocks:{filename}."""

    hostname: str
    port: Optional[int] = None
    pid: Optional[int] = None
    status: MockStatus
    jvm_args: str
    rate_limit: int = Field(ge=0)
    rate_limit_enabled: bool
    registered_at: str
    started_at: Optional[str] = None
    artifact_path: str


class MockResponse(MockConfig):
    filename: str


class MockRateLimitUpdate(BaseModel):
    enabled: bool


class MockConfigUpdate(BaseModel):
    jvm_args: Optional[str] = None
    rate_limit: Optional[int] = Field(default=None, ge=0)
