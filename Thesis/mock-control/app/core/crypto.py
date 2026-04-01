import os
from pathlib import Path
from cryptography.fernet import Fernet
from app.config import settings


def _load_or_create_key() -> bytes:
    key_path = Path(settings.fernet_key_path)
    if key_path.exists():
        return key_path.read_bytes().strip()
    # Generate and persist a new key
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    return key


_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_password(plaintext: str) -> str:
    """Encrypt a plaintext password and return a base64-encoded ciphertext string."""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext and return the plaintext password."""
    return get_fernet().decrypt(ciphertext.encode()).decode()
