"""Сервис шифрования паролей SSH: Fernet (AES-128-CBC) из пакета cryptography.

Ключ хранится в файле с правами 600 (см. `config.fernet_key_path`). Расшифрованные
значения не записываются на диск и существуют только в оперативной памяти на время
вызова `decrypt` и использования результата вызывающим кодом.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import Settings

logger = logging.getLogger(__name__)


class CryptoService:
    """Симметричное шифрование паролей перед сохранением в Redis и расшифровка перед SSH."""

    def __init__(self, settings: Settings) -> None:
        key = _load_or_create_fernet_key(Path(settings.fernet_key_path))
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """Шифрует строку; результат — токен Fernet (строка ASCII) для хранения в Redis."""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Расшифровывает токен Fernet. Пароль остаётся только в возвращаемой строке в памяти."""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Не удалось расшифровать: неверный ключ или повреждённые данные") from exc


def _load_or_create_fernet_key(path: Path) -> bytes:
    """Читает ключ из `path` или создаёт каталог, генерирует ключ и сохраняет с правами 600."""
    if path.is_file():
        raw = path.read_bytes().strip()
        if not raw:
            raise ValueError(f"Файл ключа Fernet пуст: {path}")
        return raw

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key + b"\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        logger.warning("Failed to set key file permissions to 600: %s", path, exc_info=True)
    logger.info("Created new Fernet key: %s", path)
    return key
