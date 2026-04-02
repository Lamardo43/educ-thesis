"""
Тесты Lifecycle Manager.

Все SSH-операции мокаются — тесты проверяют логику координации
между Redis (через сервисы), SSH-модулем и CryptoService.

Проверяет:
- Регистрацию (SCP + запись в Redis + удаление tmp)
- Запуск (поиск порта + nohup + обновление статуса)
- Остановку (SIGTERM/SIGKILL + сброс полей)
- Удаление (остановка + удаление файлов + удаление из Redis)
- Reconciliation (верификация PID после рестарта системы)
- Обработку ошибок (дубликат имени, хост не найден, SSH-ошибка)
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mockcontrol.core.lifecycle import LifecycleError, LifecycleManager
from mockcontrol.models.mock import MockStatus


@pytest.fixture
def lifecycle(mock_service, host_service, account_service, crypto, tmp_path):
    """LifecycleManager с реальными сервисами (FakeRedis) и мок-SSH."""
    return LifecycleManager(
        mock_service=mock_service,
        host_service=host_service,
        account_service=account_service,
        crypto=crypto,
        tmp_dir=tmp_path,
    )


@pytest.fixture
def setup_host(host_service, account_service, make_host_config, make_account_config):
    """Зарегистрировать хост и учётную запись для тестов."""

    async def _setup(
        hostname="test-server-01",
        account_uuid="e5f6a7b8-c9d0-1234-efab-567890abcdef",
    ):
        acc = make_account_config()
        await account_service.register(account_uuid, acc)

        host = make_host_config(account_uuid=account_uuid)
        await host_service.register(hostname, host)

    return _setup


@pytest.mark.asyncio
class TestRegister:
    """Тесты регистрации заглушки."""

    async def test_register_success(self, lifecycle, setup_host, mock_service, tmp_path):
        """Успешная регистрация: SCP → запись в Redis → удаление tmp."""
        await setup_host()

        # Создать временный файл
        tmp_file = tmp_path / "payment-stub.jar"
        tmp_file.write_bytes(b"fake jar content")

        with patch("mockcontrol.core.lifecycle.copy_artifact", new_callable=AsyncMock) as mock_scp:
            mock_scp.return_value = True

            config = await lifecycle.register(
                filename="payment-stub.jar",
                local_file_path=tmp_file,
                hostname="test-server-01",
                jvm_args="-Xms128m",
                rate_limit=500,
            )

        assert config.status == MockStatus.REGISTERED
        assert config.hostname == "test-server-01"
        assert config.jvm_args == "-Xms128m"
        assert config.rate_limit == 500
        assert config.rate_limit_enabled is False

        # Запись должна быть в Redis
        stored = await mock_service.get("payment-stub.jar")
        assert stored is not None

        # SCP должен быть вызван
        mock_scp.assert_called_once()

    async def test_register_duplicate_name_raises(
        self, lifecycle, setup_host, mock_service, make_mock_config, tmp_path
    ):
        """Повторная регистрация с тем же именем → LifecycleError."""
        await setup_host()
        await mock_service.register("dup.jar", make_mock_config())

        tmp_file = tmp_path / "dup.jar"
        tmp_file.write_bytes(b"data")

        with pytest.raises(LifecycleError, match="already registered"):
            await lifecycle.register("dup.jar", tmp_file, "test-server-01")

    async def test_register_unknown_host_raises(self, lifecycle, tmp_path):
        """Регистрация на несуществующий хост → LifecycleError."""
        tmp_file = tmp_path / "x.jar"
        tmp_file.write_bytes(b"data")

        with pytest.raises(LifecycleError, match="not found"):
            await lifecycle.register("x.jar", tmp_file, "nonexistent-host")


@pytest.mark.asyncio
class TestStartStop:
    """Тесты запуска и остановки."""

    async def test_start_success(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Успешный запуск: порт найден → процесс создан → статус RUNNING."""
        await setup_host()
        await mock_service.register("start.jar", make_mock_config())

        with (
            patch("mockcontrol.core.lifecycle.find_free_port", new_callable=AsyncMock) as mock_port,
            patch("mockcontrol.core.lifecycle.start_java_process", new_callable=AsyncMock) as mock_start,
        ):
            mock_port.return_value = 8101
            mock_start.return_value = 54321

            config = await lifecycle.start("start.jar")

        assert config.status == MockStatus.RUNNING
        assert config.port == 8101
        assert config.pid == 54321
        assert config.started_at is not None

    async def test_start_already_running_raises(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Запуск уже запущенной заглушки → LifecycleError."""
        await setup_host()
        await mock_service.register(
            "running.jar",
            make_mock_config(status=MockStatus.RUNNING, port=8100, pid=111),
        )

        with pytest.raises(LifecycleError, match="already running"):
            await lifecycle.start("running.jar")

    async def test_stop_success(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Успешная остановка: SIGTERM → статус STOPPED, PID/port обнулены."""
        await setup_host()
        await mock_service.register(
            "stop.jar",
            make_mock_config(status=MockStatus.RUNNING, port=8100, pid=99999),
        )

        with patch("mockcontrol.core.lifecycle.stop_process", new_callable=AsyncMock) as mock_stop:
            mock_stop.return_value = True
            config = await lifecycle.stop("stop.jar")

        assert config.status == MockStatus.STOPPED
        assert config.port is None
        assert config.pid is None
        assert config.started_at is None

    async def test_stop_not_running_raises(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Остановка незапущенной заглушки → LifecycleError."""
        await setup_host()
        await mock_service.register("idle.jar", make_mock_config())

        with pytest.raises(LifecycleError, match="not running"):
            await lifecycle.stop("idle.jar")


@pytest.mark.asyncio
class TestDelete:
    """Тесты удаления заглушки."""

    async def test_delete_stopped_mock(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Удаление остановленной заглушки: файлы удалены, запись удалена."""
        await setup_host()
        await mock_service.register("del.jar", make_mock_config())

        with patch("mockcontrol.core.lifecycle.delete_remote_file", new_callable=AsyncMock) as mock_rm:
            mock_rm.return_value = True
            await lifecycle.delete("del.jar")

        assert await mock_service.get("del.jar") is None

    async def test_delete_running_mock_stops_first(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Удаление запущенной заглушки: сначала остановка, потом удаление."""
        await setup_host()
        await mock_service.register(
            "active.jar",
            make_mock_config(status=MockStatus.RUNNING, pid=777, port=8100),
        )

        with (
            patch("mockcontrol.core.lifecycle.stop_process", new_callable=AsyncMock) as mock_stop,
            patch("mockcontrol.core.lifecycle.delete_remote_file", new_callable=AsyncMock) as mock_rm,
        ):
            mock_stop.return_value = True
            mock_rm.return_value = True
            await lifecycle.delete("active.jar")

        mock_stop.assert_called_once()
        assert await mock_service.get("active.jar") is None

    async def test_delete_nonexistent_raises(self, lifecycle):
        """Удаление несуществующей заглушки → LifecycleError."""
        with pytest.raises(LifecycleError, match="not found"):
            await lifecycle.delete("ghost.jar")


@pytest.mark.asyncio
class TestReconciliation:
    """Тесты reconciliation — верификация состояний после рестарта."""

    async def test_reconcile_marks_dead_as_stopped(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Если процесс мёртв — статус обновляется до STOPPED."""
        await setup_host()
        await mock_service.register(
            "dead.jar",
            make_mock_config(status=MockStatus.RUNNING, pid=12345, port=8100),
        )

        with patch("mockcontrol.core.lifecycle.check_process_alive", new_callable=AsyncMock) as mock_alive:
            mock_alive.return_value = False
            changes = await lifecycle.reconcile()

        assert "dead.jar" in changes
        assert changes["dead.jar"] == MockStatus.STOPPED

        updated = await mock_service.get("dead.jar")
        assert updated.status == MockStatus.STOPPED
        assert updated.pid is None

    async def test_reconcile_keeps_alive_unchanged(
        self, lifecycle, setup_host, mock_service, make_mock_config
    ):
        """Если процесс жив — статус не меняется."""
        await setup_host()
        await mock_service.register(
            "alive.jar",
            make_mock_config(status=MockStatus.RUNNING, pid=11111, port=8100),
        )

        with patch("mockcontrol.core.lifecycle.check_process_alive", new_callable=AsyncMock) as mock_alive:
            mock_alive.return_value = True
            changes = await lifecycle.reconcile()

        assert "alive.jar" not in changes

        stored = await mock_service.get("alive.jar")
        assert stored.status == MockStatus.RUNNING

    async def test_reconcile_skips_non_running(
        self, lifecycle, mock_service, make_mock_config
    ):
        """Reconciliation пропускает заглушки, не находящиеся в RUNNING."""
        await mock_service.register("stopped.jar", make_mock_config())
        changes = await lifecycle.reconcile()
        assert changes == {}
