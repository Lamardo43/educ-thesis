"""
Тесты CryptoService и утилит (validate_artifact_filename, port_finder).
"""

import pytest

from mockcontrol.core.crypto import CryptoService, CryptoServiceError
from mockcontrol.utils import validate_artifact_filename
from mockcontrol.utils.port_finder import (
    find_free_in_range,
    parse_occupied_ports,
    validate_port_range,
)


# =====================================================================
# CryptoService
# =====================================================================


class TestCryptoService:
    """Тесты шифрования/дешифрования паролей."""

    def test_encrypt_decrypt_roundtrip(self, crypto: CryptoService):
        """Зашифрованный текст расшифровывается обратно."""
        original = "my_secret_password_123!@#"
        encrypted = crypto.encrypt(original)
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == original

    def test_encrypted_differs_from_plaintext(self, crypto: CryptoService):
        """Зашифрованный текст не совпадает с открытым."""
        original = "password"
        encrypted = crypto.encrypt(original)
        assert encrypted != original

    def test_different_encryptions_differ(self, crypto: CryptoService):
        """Два шифрования одного текста дают разные результаты (nonce/IV)."""
        enc1 = crypto.encrypt("same")
        enc2 = crypto.encrypt("same")
        assert enc1 != enc2  # Fernet использует случайный IV

    def test_decrypt_invalid_token_raises(self, crypto: CryptoService):
        """Некорректный ciphertext → CryptoServiceError."""
        with pytest.raises(CryptoServiceError, match="invalid token"):
            crypto.decrypt("not-a-valid-fernet-token")

    def test_key_file_created_with_600_permissions(self, tmp_path):
        """Файл ключа создаётся с правами 600."""
        key_path = tmp_path / "new_key.key"
        CryptoService(key_path)

        assert key_path.exists()
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_key_reloaded_on_second_init(self, tmp_path):
        """Повторная инициализация загружает существующий ключ."""
        key_path = tmp_path / "reuse_key.key"
        cs1 = CryptoService(key_path)
        encrypted = cs1.encrypt("test")

        cs2 = CryptoService(key_path)
        decrypted = cs2.decrypt(encrypted)
        assert decrypted == "test"

    def test_unicode_password(self, crypto: CryptoService):
        """Поддержка Unicode-паролей (кириллица, спецсимволы)."""
        original = "пароль_密码_🔑"
        encrypted = crypto.encrypt(original)
        assert crypto.decrypt(encrypted) == original


# =====================================================================
# validate_artifact_filename
# =====================================================================


class TestValidateArtifactFilename:
    """Тесты валидации имени файла артефакта."""

    @pytest.mark.parametrize("filename", [
        "payment-service-stub.jar",
        "auth_service.war",
        "my.service.v2.jar",
        "stub123.jar",
    ])
    def test_valid_filenames(self, filename):
        assert validate_artifact_filename(filename) is None

    @pytest.mark.parametrize("filename,expected_error", [
        ("", "empty"),
        ("file.txt", "Unsupported extension"),
        ("file.py", "Unsupported extension"),
        ("no-extension", "Unsupported extension"),
        ("bad name.jar", "invalid characters"),
        ("path/file.jar", "invalid characters"),
    ])
    def test_invalid_filenames(self, filename, expected_error):
        error = validate_artifact_filename(filename)
        assert error is not None
        assert expected_error.lower() in error.lower()


# =====================================================================
# port_finder
# =====================================================================


class TestPortFinder:
    """Тесты утилит для работы с портами."""

    def test_validate_port_range_valid(self):
        assert validate_port_range(8100, 8200) is None

    def test_validate_port_range_min_too_low(self):
        error = validate_port_range(80, 8200)
        assert "1024" in error

    def test_validate_port_range_inverted(self):
        error = validate_port_range(9000, 8000)
        assert "must be <=" in error

    def test_parse_occupied_ports(self):
        ss_output = """LISTEN  0  128  0.0.0.0:8100  0.0.0.0:*
LISTEN  0  128  0.0.0.0:8101  0.0.0.0:*
LISTEN  0  128  [::]:22       [::]:*
LISTEN  0  128  127.0.0.1:6379 0.0.0.0:*"""
        ports = parse_occupied_ports(ss_output)
        assert ports == {8100, 8101, 22, 6379}

    def test_parse_empty_output(self):
        assert parse_occupied_ports("") == set()

    def test_find_free_in_range_first_available(self):
        occupied = {8100, 8101, 8102}
        port = find_free_in_range(occupied, 8100, 8110)
        assert port == 8103

    def test_find_free_in_range_all_occupied(self):
        occupied = {8100, 8101, 8102}
        port = find_free_in_range(occupied, 8100, 8102)
        assert port is None

    def test_find_free_in_range_none_occupied(self):
        port = find_free_in_range(set(), 8100, 8100)
        assert port == 8100
