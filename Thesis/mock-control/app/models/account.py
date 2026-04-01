from datetime import datetime
from pydantic import BaseModel


class AccountRecord(BaseModel):
    uuid: str
    username: str
    password_enc: str          # Fernet-encrypted, stored in Redis
    description: str = ""
    created_at: datetime


class CreateAccountRequest(BaseModel):
    username: str
    password: str              # Plaintext — encrypted before saving
    description: str = ""
