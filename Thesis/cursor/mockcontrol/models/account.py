from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    username: str
    password: str = Field(description="Plain password; encrypted before storage")
    description: str = ""


class AccountConfig(BaseModel):
    """Account record as stored in Redis hash accounts:{uuid}."""

    username: str
    password_enc: str
    description: str
    created_at: str


class AccountResponse(BaseModel):
    username: str
    uuid: str
    description: str
    created_at: str
