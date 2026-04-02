"""
Crypto Service — симметричное шифрование паролей SSH.

Использует алгоритм AES-128 в режиме CBC (Fernet) из библиотеки cryptography.
Ключ хранится на файловой системе сервера в файле с правами 600,
вне Redis, что защищает пароли при утечке дампа БД.

Расшифрованный пароль существует только в оперативной памяти
процесса на время установки SSH-соединения.
"""

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class CryptoServiceError(Exception):
    """Ошибка Crypto Service."""


class CryptoService:
    """Шифрование и дешифрование паролей на основе Fernet."""

    def __init__(self, key_path: Path) -> None:
        """
        Args:
            key_path: Путь к файлу с Fernet-ключом.
                      Если файл отсутствует — генерируется новый ключ.
        """
        self._key_path = key_path
        self._fernet = Fernet(self._load_or_generate_key())

    def _load_or_generate_key(self) -> bytes:
        """
        Загрузить существующий ключ или сгенерировать новый.

        При генерации нового ключа файл создаётся с правами 600
        (чтение/запись только для владельца).
        """
        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
            logger.info("Loaded Fernet key from %s", self._key_path)
            return key

        # Генерация нового ключа
        key = Fernet.generate_key()

        # Создать файл с правами 600
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.write_bytes(key)
        os.chmod(self._key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

        logger.info("Generated new Fernet key at %s", self._key_path)
        return key

    def encrypt(self, plaintext: str) -> str:
        """
        Зашифровать строку (пароль) и вернуть Base64-строку.

        Args:
            plaintext: Открытый текст пароля.

        Returns:
            Зашифрованная строка, пригодная для хранения в Redis.
        """
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """
        Расшифровать строку, полученную из Redis.

        Args:
            ciphertext: Зашифрованная Base64-строка.

        Returns:
            Открытый текст пароля.

        Raises:
            CryptoServiceError: Если дешифрование не удалось
                                (повреждённые данные или неверный ключ).
        """
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except InvalidToken as exc:
            raise CryptoServiceError(
                "Failed to decrypt: invalid token or wrong key"
            ) from exc
