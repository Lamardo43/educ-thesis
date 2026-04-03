"""Кастомные исключения домена MockControl."""


class MockControlError(Exception):
    """Базовое исключение MockControl."""


class MockNotFoundError(MockControlError):
    """Заглушка не найдена в реестре или Redis."""


class MockAlreadyExistsError(MockControlError):
    """Заглушка с таким именем файла уже зарегистрирована."""


class HostNotFoundError(MockControlError):
    """Хост не найден в конфигурации."""


class AccountNotFoundError(MockControlError):
    """Учётная запись SSH не найдена."""


class HostHasMocksError(MockControlError):
    """У хоста есть привязанные заглушки; операция запрещена."""


class AccountInUseError(MockControlError):
    """Учётная запись используется хостом; удаление запрещено."""


class MockNotRunningError(MockControlError):
    """Заглушка не в состоянии RUNNING; операция недоступна."""


class MockAlreadyRunningError(MockControlError):
    """Заглушка уже запущена."""


class SSHConnectionError(MockControlError):
    """Ошибка установки или использования SSH-соединения."""


class SCPError(MockControlError):
    """Ошибка передачи файла по SCP."""


class DecryptionError(MockControlError):
    """Не удалось расшифровать сохранённые данные (например, пароль учётной записи)."""


class PortAllocationError(MockControlError):
    """В заданном диапазоне нет свободного порта."""


class MockProcessError(MockControlError):
    """Ошибка запуска, проверки или остановки процесса заглушки."""


class ArtifactOperationError(MockControlError):
    """Ошибка копирования, прав доступа или удаления артефакта на хосте."""
