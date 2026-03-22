from enum import Enum
from datetime import datetime
from pydantic import BaseModel


class HostStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class HostRecord(BaseModel):
    hostname: str
    ssh_port: int = 22
    account_uuid: str
    working_dir: str
    java_path: str = "/usr/bin/java"
    mock_port_min: int = 8100
    mock_port_max: int = 8200
    description: str = ""
    status: HostStatus = HostStatus.UNKNOWN
    last_checked_at: datetime | None = None


class CreateHostRequest(BaseModel):
    hostname: str
    ssh_port: int = 22
    account_uuid: str
    working_dir: str
    java_path: str = "/usr/bin/java"
    mock_port_min: int = 8100
    mock_port_max: int = 8200
    description: str = ""
