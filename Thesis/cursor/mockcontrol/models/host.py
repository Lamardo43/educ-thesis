from enum import Enum

from pydantic import BaseModel, Field


class HostStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class HostCreate(BaseModel):
    """Input for adding a host (modal fields from settings UI)."""

    hostname: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    account_uuid: str
    working_dir: str
    java_path: str
    mock_port_min: int = Field(ge=1, le=65535)
    mock_port_max: int = Field(ge=1, le=65535)
    description: str = ""


class HostConfig(BaseModel):
    """Full host record as stored in Redis hash hosts:{hostname}."""

    ssh_port: int = Field(ge=1, le=65535)
    account_uuid: str
    working_dir: str
    java_path: str
    mock_port_min: int = Field(ge=1, le=65535)
    mock_port_max: int = Field(ge=1, le=65535)
    description: str
    status: HostStatus
    last_checked_at: str


class HostResponse(HostConfig):
    hostname: str
