"""
Тесты сервисного слоя (MockService, HostService, AccountService, SettingsService).

Проверяет:
- CRUD-операции (register, get, set, delete)
- Уникальность при регистрации (SADD возвращает 0)
- Массовое чтение через Pipeline (list_configs)
- Атомарное обновление через WATCH/MULTI/EXEC (update_field)
- Агрегация по статусам (count_by_status)
- Фильтрация (find_by_hostname)
- Глобальные настройки (ensure_defaults, partial_update)
"""

import pytest

from mockcontrol.models.host import HostStatus
from mockcontrol.models.mock import MockConfig, MockStatus
from mockcontrol.models.settings import GlobalSettings
from mockcontrol.services.account_service import AccountService
from mockcontrol.services.host_service import HostService
from mockcontrol.services.mock_service import MockService
from mockcontrol.services.settings_service import SettingsService


# =====================================================================
# MockService
# =====================================================================


@pytest.mark.asyncio
class TestMockServiceCRUD:
    """Базовые операции с заглушками."""

    async def test_register_and_get(self, mock_service: MockService, make_mock_config):
        """Зарегистрированная заглушка читается корректно."""
        config = make_mock_config(hostname="server-01")
        result = await mock_service.register("stub.jar", config)
        assert result is True

        retrieved = await mock_service.get("stub.jar")
        assert retrieved is not None
        assert retrieved.hostname == "server-01"
        assert retrieved.status == MockStatus.REGISTERED

    async def test_register_duplicate_returns_false(
        self, mock_service: MockService, make_mock_config
    ):
        """Повторная регистрация с тем же именем возвращает False."""
        config = make_mock_config()
        await mock_service.register("dup.jar", config)
        result = await mock_service.register("dup.jar", config)
        assert result is False

    async def test_get_nonexistent_returns_none(self, mock_service: MockService):
        """Несуществующая заглушка → None."""
        assert await mock_service.get("ghost.jar") is None

    async def test_exists(self, mock_service: MockService, make_mock_config):
        """exists() возвращает True/False корректно."""
        assert await mock_service.exists("x.jar") is False
        await mock_service.register("x.jar", make_mock_config())
        assert await mock_service.exists("x.jar") is True

    async def test_delete_removes_from_registry(
        self, mock_service: MockService, make_mock_config
    ):
        """Удаление убирает запись и из реестра, и из хранилища."""
        await mock_service.register("del.jar", make_mock_config())
        await mock_service.delete("del.jar")

        assert await mock_service.get("del.jar") is None
        assert await mock_service.exists("del.jar") is False

    async def test_set_overwrites(self, mock_service: MockService, make_mock_config):
        """set() перезаписывает существующую конфигурацию."""
        await mock_service.register("ow.jar", make_mock_config(rate_limit=100))
        await mock_service.set("ow.jar", make_mock_config(rate_limit=999))

        updated = await mock_service.get("ow.jar")
        assert updated.rate_limit == 999


@pytest.mark.asyncio
class TestMockServiceBulk:
    """Массовые операции и агрегация."""

    async def test_list_all_sorted(self, mock_service: MockService, make_mock_config):
        """list_all() возвращает отсортированный список имён."""
        for name in ["c.jar", "a.jar", "b.jar"]:
            await mock_service.register(name, make_mock_config())

        names = await mock_service.list_all()
        assert names == ["a.jar", "b.jar", "c.jar"]

    async def test_list_configs_pipeline(self, mock_service: MockService, make_mock_config):
        """list_configs() возвращает все конфигурации одним вызовом."""
        await mock_service.register("p1.jar", make_mock_config(hostname="h1"))
        await mock_service.register("p2.jar", make_mock_config(hostname="h2"))

        configs = await mock_service.list_configs()
        assert len(configs) == 2
        assert configs["p1.jar"].hostname == "h1"
        assert configs["p2.jar"].hostname == "h2"

    async def test_count_by_status(self, mock_service: MockService, make_mock_config):
        """count_by_status() подсчитывает заглушки по статусам."""
        await mock_service.register("r1.jar", make_mock_config(status=MockStatus.RUNNING))
        await mock_service.register("r2.jar", make_mock_config(status=MockStatus.RUNNING))
        await mock_service.register("s1.jar", make_mock_config(status=MockStatus.STOPPED))

        counts = await mock_service.count_by_status()
        assert counts["RUNNING"] == 2
        assert counts["STOPPED"] == 1

    async def test_find_by_hostname(self, mock_service: MockService, make_mock_config):
        """find_by_hostname() фильтрует заглушки по хосту."""
        await mock_service.register("a.jar", make_mock_config(hostname="host-a"))
        await mock_service.register("b.jar", make_mock_config(hostname="host-b"))
        await mock_service.register("c.jar", make_mock_config(hostname="host-a"))

        on_a = await mock_service.find_by_hostname("host-a")
        assert len(on_a) == 2
        assert "a.jar" in on_a
        assert "c.jar" in on_a


@pytest.mark.asyncio
class TestMockServiceUpdateField:
    """Атомарное обновление полей."""

    async def test_update_single_field(self, mock_service: MockService, make_mock_config):
        """Обновление одного поля сохраняет остальные."""
        await mock_service.register("upd.jar", make_mock_config(rate_limit=100))

        updated = await mock_service.update_field("upd.jar", rate_limit=999)
        assert updated is not None
        assert updated.rate_limit == 999
        assert updated.hostname == "test-server-01"  # Не изменилось

    async def test_update_multiple_fields(self, mock_service: MockService, make_mock_config):
        """Обновление нескольких полей одновременно."""
        await mock_service.register("multi.jar", make_mock_config())

        updated = await mock_service.update_field(
            "multi.jar",
            status=MockStatus.RUNNING,
            port=8101,
            pid=12345,
        )
        assert updated.status == MockStatus.RUNNING
        assert updated.port == 8101
        assert updated.pid == 12345

    async def test_update_nonexistent_returns_none(self, mock_service: MockService):
        """Обновление несуществующей заглушки → None."""
        result = await mock_service.update_field("nope.jar", status=MockStatus.ERROR)
        assert result is None


# =====================================================================
# HostService
# =====================================================================


@pytest.mark.asyncio
class TestHostService:
    """Операции с хостами."""

    async def test_register_and_get(self, host_service: HostService, make_host_config):
        config = make_host_config(working_dir="/opt/stubs")
        assert await host_service.register("10.0.0.1", config) is True

        retrieved = await host_service.get("10.0.0.1")
        assert retrieved.working_dir == "/opt/stubs"

    async def test_duplicate_register(self, host_service: HostService, make_host_config):
        config = make_host_config()
        await host_service.register("dup-host", config)
        assert await host_service.register("dup-host", config) is False

    async def test_count_by_status(self, host_service: HostService, make_host_config):
        await host_service.register("h1", make_host_config(status=HostStatus.AVAILABLE))
        await host_service.register("h2", make_host_config(status=HostStatus.AVAILABLE))
        await host_service.register("h3", make_host_config(status=HostStatus.UNAVAILABLE))

        counts = await host_service.count_by_status()
        assert counts["AVAILABLE"] == 2
        assert counts["UNAVAILABLE"] == 1

    async def test_count_available(self, host_service: HostService, make_host_config):
        await host_service.register("av", make_host_config(status=HostStatus.AVAILABLE))
        await host_service.register("un", make_host_config(status=HostStatus.UNAVAILABLE))
        assert await host_service.count_available() == 1

    async def test_update_field(self, host_service: HostService, make_host_config):
        await host_service.register("upd-h", make_host_config())
        updated = await host_service.update_field("upd-h", status=HostStatus.UNAVAILABLE)
        assert updated.status == HostStatus.UNAVAILABLE


# =====================================================================
# AccountService
# =====================================================================


@pytest.mark.asyncio
class TestAccountService:
    """Операции с учётными записями."""

    async def test_register_and_get(
        self, account_service: AccountService, make_account_config
    ):
        config = make_account_config(username="testuser")
        assert await account_service.register("uuid-1", config) is True

        retrieved = await account_service.get("uuid-1")
        assert retrieved.username == "testuser"

    async def test_list_configs(self, account_service: AccountService, make_account_config):
        await account_service.register("u1", make_account_config(username="user1"))
        await account_service.register("u2", make_account_config(username="user2"))

        configs = await account_service.list_configs()
        assert len(configs) == 2

    async def test_delete(self, account_service: AccountService, make_account_config):
        await account_service.register("del-u", make_account_config())
        await account_service.delete("del-u")
        assert await account_service.get("del-u") is None


# =====================================================================
# SettingsService
# =====================================================================


@pytest.mark.asyncio
class TestSettingsService:
    """Операции с глобальными настройками."""

    async def test_get_returns_defaults_when_missing(
        self, settings_service: SettingsService
    ):
        """При отсутствии записи возвращаются значения по умолчанию."""
        settings = await settings_service.get()
        assert settings.rate_limit_window_size == 1
        assert settings.host_check_interval_sec == 30

    async def test_ensure_defaults_creates_once(
        self, settings_service: SettingsService
    ):
        """ensure_defaults создаёт запись только при первом вызове."""
        s1 = await settings_service.ensure_defaults()
        assert s1.proxy_timeout_sec == 10

        # Изменить и вызвать снова — не перезапишет
        await settings_service.set(GlobalSettings(proxy_timeout_sec=99))
        s2 = await settings_service.ensure_defaults()
        assert s2.proxy_timeout_sec == 99  # Сохранённое значение

    async def test_set_and_get(self, settings_service: SettingsService):
        custom = GlobalSettings(
            rate_limit_window_size=5,
            host_check_interval_sec=60,
            proxy_timeout_sec=30,
            log_retention_lines=5000,
        )
        await settings_service.set(custom)

        retrieved = await settings_service.get()
        assert retrieved.rate_limit_window_size == 5
        assert retrieved.log_retention_lines == 5000

    async def test_partial_update(self, settings_service: SettingsService):
        await settings_service.ensure_defaults()

        updated = await settings_service.partial_update(proxy_timeout_sec=42)
        assert updated.proxy_timeout_sec == 42
        assert updated.rate_limit_window_size == 1  # Не изменилось

    async def test_exists(self, settings_service: SettingsService):
        assert await settings_service.exists() is False
        await settings_service.ensure_defaults()
        assert await settings_service.exists() is True
