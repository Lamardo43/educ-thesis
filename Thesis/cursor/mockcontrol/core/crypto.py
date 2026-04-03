"""Симметричное шифрование паролей SSH (Fernet)."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from mockcontrol.core.exceptions import DecryptionError

logger = logging.getLogger(__name__)


class CryptoService:
    """Шифрование и дешифрование паролей на основе Fernet."""

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path
        self._fernet = Fernet(self._load_or_generate_key())

    def _load_or_generate_key(self) -> bytes:
        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
            logger.info("Loaded Fernet key from %s", self._key_path)
            return key

        key = Fernet.generate_key()
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.write_bytes(key)
        os.chmod(self._key_path, stat.S_IRUSR | stat.S_IWUSR)
        logger.info("Generated new Fernet key at %s", self._key_path)
        return key

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except InvalidToken as exc:
            raise DecryptionError(
                "Не удалось расшифровать данные: повреждённый токен или неверный ключ"
            ) from exc
