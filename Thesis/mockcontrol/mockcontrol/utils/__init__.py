"""Вспомогательные утилиты."""

import re

# Допустимые расширения артефактов
ALLOWED_EXTENSIONS = frozenset({".jar", ".war"})

# Имя файла: буквы, цифры, дефис, подчёркивание, точка
_FILENAME_RE = re.compile(r"^[\w\-\.]+$")


def validate_artifact_filename(filename: str) -> str | None:
    """
    Проверить корректность имени файла артефакта.

    Returns:
        None если валидно, строка ошибки если нет.
    """
    if not filename:
        return "Filename is empty"

    if not _FILENAME_RE.match(filename):
        return "Filename contains invalid characters"

    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext.lower() not in ALLOWED_EXTENSIONS:
        return f"Unsupported extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"

    return None
