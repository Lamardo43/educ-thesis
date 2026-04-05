"""Кастомные исключения предметной области MockControl."""


class MockNotFoundError(Exception):
    """Заглушка не найдена в реестре или Redis."""


class MockAlreadyExistsError(Exception):
    """Заглушка с таким именем файла уже зарегистрирована."""


class HostNotFoundError(Exception):
    """Хост не найден в конфигурации."""


class HostAlreadyExistsError(Exception):
    """Хост с таким hostname уже есть в реестре."""


class AccountNotFoundError(Exception):
    """Учётная запись SSH не найдена."""


class HostHasMocksError(Exception):
    """У хоста есть привязанные заглушки; операция запрещена."""

    def __init__(self, mocks: list[str]) -> None:
        self.mocks = list(mocks)
        listed = ", ".join(self.mocks)
        super().__init__(f"У хоста есть привязанные заглушки: {listed}")


class AccountInUseError(Exception):
    """Учётная запись используется хостом; удаление запрещено."""

    def __init__(self, hosts: list[str]) -> None:
        self.hosts = list(hosts)
        listed = ", ".join(self.hosts)
        super().__init__(f"Учётная запись используется хостами: {listed}")


class MockNotRunningError(Exception):
    """Заглушка не в состоянии RUNNING."""


class MockAlreadyRunningError(Exception):
    """Заглушка уже запущена."""


class SSHConnectionError(Exception):
    """Ошибка установки или использования SSH-соединения."""


class SCPError(Exception):
    """Ошибка передачи файла по SCP."""


class InvalidMockFileError(Exception):
    """Некорректное имя файла артефакта или пустая загрузка."""


class LifecycleOperationError(Exception):
    """Сбой операции жизненного цикла (порты, процесс Java, файловая система)."""
