"""Settings Module — CRUD хостов, учётных записей SSH и глобальных настроек (Redis Hash, ТЗ)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from redis.asyncio import Redis

from core.crypto import CryptoService
from core.exceptions import (
    AccountInUseError,
    AccountNotFoundError,
    HostAlreadyExistsError,
    HostHasMocksError,
    HostNotFoundError,
)
from models.account import AccountConfig, AccountCreate, AccountResponse, AccountUpdate
from models.host import HostConfig, HostCreate, HostResponse, HostStatus, HostUpdate
from models.settings import GlobalSettings, GlobalSettingsUpdate

logger = logging.getLogger(__name__)

HOSTS_REGISTRY_KEY = "hosts:registry"
ACCOUNTS_REGISTRY_KEY = "accounts:registry"
MOCKS_REGISTRY_KEY = "mocks:registry"
SETTINGS_GLOBAL_KEY = "settings:global"

_DEFAULT_GLOBAL_HASH: dict[str, str] = {
    "rate_limit_window_size": "1",
    "host_check_interval_sec": "30",
    "proxy_timeout_sec": "10",
    "log_retention_lines": "1000",
}


def _host_key(hostname: str) -> str:
    return f"hosts:{hostname}"


def _account_key(account_uuid: str) -> str:
    return f"accounts:{account_uuid}"


def _mock_key(filename: str) -> str:
    return f"mocks:{filename}"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host_from_hash(hostname: str, data: dict[str, str]) -> HostResponse:
    if not data:
        raise HostNotFoundError(hostname)
    return HostResponse(
        hostname=hostname,
        ssh_port=int(data["ssh_port"]),
        account_uuid=data["account_uuid"],
        working_dir=data["working_dir"],
        java_path=data["java_path"],
        mock_port_min=int(data["mock_port_min"]),
        mock_port_max=int(data["mock_port_max"]),
        description=data.get("description", ""),
        status=HostStatus(data.get("status", HostStatus.UNKNOWN.value)),
        last_checked_at=data.get("last_checked_at", ""),
    )


def _host_to_redis_hash(data: HostCreate | HostResponse, *, status: str, last_checked_at: str) -> dict[str, str]:
    return {
        "ssh_port": str(data.ssh_port),
        "account_uuid": data.account_uuid,
        "working_dir": data.working_dir,
        "java_path": data.java_path,
        "mock_port_min": str(data.mock_port_min),
        "mock_port_max": str(data.mock_port_max),
        "description": data.description,
        "status": status,
        "last_checked_at": last_checked_at,
    }


def _account_from_hash(account_uuid: str, data: dict[str, str]) -> AccountConfig:
    if not data:
        raise AccountNotFoundError(account_uuid)
    return AccountConfig(
        username=data["username"],
        password_enc=data["password_enc"],
        description=data.get("description", ""),
        created_at=data.get("created_at", ""),
    )


def _account_to_response(account_uuid: str, data: dict[str, str]) -> AccountResponse:
    return AccountResponse(
        uuid=account_uuid,
        username=data["username"],
        description=data.get("description", ""),
        created_at=data.get("created_at", ""),
    )


def _global_from_hash(data: dict[str, str]) -> GlobalSettings:
    return GlobalSettings(
        rate_limit_window_size=int(data["rate_limit_window_size"]),
        host_check_interval_sec=int(data["host_check_interval_sec"]),
        proxy_timeout_sec=int(data["proxy_timeout_sec"]),
        log_retention_lines=int(data["log_retention_lines"]),
    )


def _global_to_redis_hash(settings: GlobalSettings) -> dict[str, str]:
    return {
        "rate_limit_window_size": str(settings.rate_limit_window_size),
        "host_check_interval_sec": str(settings.host_check_interval_sec),
        "proxy_timeout_sec": str(settings.proxy_timeout_sec),
        "log_retention_lines": str(settings.log_retention_lines),
    }


class SettingsService:
    """CRUD для хостов, учётных записей и глобальных настроек в Redis."""

    def __init__(self, redis: Redis, crypto: CryptoService) -> None:
        self._redis = redis
        self._crypto = crypto

    async def _account_exists(self, account_uuid: str) -> bool:
        return bool(await self._redis.exists(_account_key(account_uuid)))

    async def _mocks_on_host(self, hostname: str) -> list[str]:
        filenames = await self._redis.smembers(MOCKS_REGISTRY_KEY)
        if not filenames:
            return []
        names_sorted = sorted(filenames)
        pipe = self._redis.pipeline()
        for fn in names_sorted:
            pipe.hget(_mock_key(fn), "hostname")
        hostnames = await pipe.execute()
        out: list[str] = []
        for fn, h in zip(names_sorted, hostnames):
            if h == hostname:
                out.append(fn)
        return out

    async def _hosts_using_account(self, account_uuid: str) -> list[str]:
        hostnames = await self._redis.smembers(HOSTS_REGISTRY_KEY)
        if not hostnames:
            return []
        sorted_hosts = sorted(hostnames)
        pipe = self._redis.pipeline()
        for hn in sorted_hosts:
            pipe.hget(_host_key(hn), "account_uuid")
        uuids = await pipe.execute()
        out: list[str] = []
        for hn, au in zip(sorted_hosts, uuids):
            if au == account_uuid:
                out.append(hn)
        return out

    async def list_hosts(self) -> list[HostResponse]:
        """Возвращает все хосты: ``SMEMBERS hosts:registry``, затем ``HGETALL`` для каждого ключа через pipeline."""
        names = await self._redis.smembers(HOSTS_REGISTRY_KEY)
        if not names:
            return []
        sorted_names = sorted(names)
        pipe = self._redis.pipeline()
        for hn in sorted_names:
            pipe.hgetall(_host_key(hn))
        rows: list[dict[str, str]] = await pipe.execute()
        result: list[HostResponse] = []
        for hn, row in zip(sorted_names, rows):
            if not row:
                logger.warning("Hosts registry has orphan entry without Hash: %s", hn)
                continue
            result.append(_host_from_hash(hn, row))
        return result

    async def get_host(self, hostname: str) -> HostResponse:
        """Читает Hash ``hosts:{hostname}``; если ключа нет — ``HostNotFoundError``."""
        data = await self._redis.hgetall(_host_key(hostname))
        return _host_from_hash(hostname, data)

    async def create_host(self, data: HostCreate) -> HostResponse:
        """Регистрирует хост: уникальность через ``SISMEMBER``, затем ``HSET`` и ``SADD`` в реестр."""
        if await self._redis.sismember(HOSTS_REGISTRY_KEY, data.hostname):
            raise HostAlreadyExistsError(data.hostname)
        if not await self._account_exists(data.account_uuid):
            raise AccountNotFoundError(data.account_uuid)
        key = _host_key(data.hostname)
        mapping = _host_to_redis_hash(
            data,
            status=HostStatus.UNKNOWN.value,
            last_checked_at="",
        )
        await self._redis.hset(key, mapping=mapping)
        await self._redis.sadd(HOSTS_REGISTRY_KEY, data.hostname)
        return _host_from_hash(data.hostname, mapping)

    async def update_host(self, hostname: str, data: HostUpdate) -> HostResponse:
        """Обновляет только переданные поля Hash существующего хоста (``HSET``)."""
        key = _host_key(hostname)
        current = await self._redis.hgetall(key)
        if not current:
            raise HostNotFoundError(hostname)
        if data.account_uuid is not None and not await self._account_exists(data.account_uuid):
            raise AccountNotFoundError(data.account_uuid)
        updates: dict[str, str] = {}
        payload = data.model_dump(exclude_unset=True)
        if "ssh_port" in payload:
            updates["ssh_port"] = str(payload["ssh_port"])
        if "account_uuid" in payload:
            updates["account_uuid"] = payload["account_uuid"]
        if "working_dir" in payload:
            updates["working_dir"] = payload["working_dir"]
        if "java_path" in payload:
            updates["java_path"] = payload["java_path"]
        if "mock_port_min" in payload:
            updates["mock_port_min"] = str(payload["mock_port_min"])
        if "mock_port_max" in payload:
            updates["mock_port_max"] = str(payload["mock_port_max"])
        if "description" in payload:
            updates["description"] = payload["description"] or ""
        if updates:
            await self._redis.hset(key, mapping=updates)
        merged = {**current, **updates}
        return _host_from_hash(hostname, merged)

    async def delete_host(self, hostname: str) -> None:
        """Удаляет хост, если к нему не привязаны заглушки; иначе ``HostHasMocksError`` с перечислением файлов."""
        if not await self._redis.exists(_host_key(hostname)):
            raise HostNotFoundError(hostname)
        mocks = await self._mocks_on_host(hostname)
        if mocks:
            raise HostHasMocksError(mocks)
        key = _host_key(hostname)
        await self._redis.delete(key)
        await self._redis.srem(HOSTS_REGISTRY_KEY, hostname)

    async def list_accounts(self) -> list[AccountResponse]:
        """Список учётных записей; в ответе нет поля ``password_enc``."""
        ids = await self._redis.smembers(ACCOUNTS_REGISTRY_KEY)
        if not ids:
            return []
        sorted_ids = sorted(ids)
        pipe = self._redis.pipeline()
        for uid in sorted_ids:
            pipe.hgetall(_account_key(uid))
        rows: list[dict[str, str]] = await pipe.execute()
        result: list[AccountResponse] = []
        for uid, row in zip(sorted_ids, rows):
            if not row:
                logger.warning("Accounts registry has orphan entry without Hash: %s", uid)
                continue
            result.append(_account_to_response(uid, row))
        return result

    async def get_account(self, account_uuid: str) -> AccountConfig:
        """Полный Hash ``accounts:{uuid}`` (включая ``password_enc``)."""
        data = await self._redis.hgetall(_account_key(account_uuid))
        return _account_from_hash(account_uuid, data)

    async def create_account(self, data: AccountCreate) -> AccountResponse:
        """Создаёт UUID, шифрует пароль, ``HSET`` и ``SADD`` в ``accounts:registry``."""
        new_uuid = str(uuid.uuid4())
        key = _account_key(new_uuid)
        password_enc = self._crypto.encrypt(data.password)
        created_at = _utc_iso()
        mapping: dict[str, str] = {
            "username": data.username,
            "password_enc": password_enc,
            "description": data.description or "",
            "created_at": created_at,
        }
        await self._redis.hset(key, mapping=mapping)
        await self._redis.sadd(ACCOUNTS_REGISTRY_KEY, new_uuid)
        return AccountResponse(
            uuid=new_uuid,
            username=data.username,
            description=data.description or "",
            created_at=created_at,
        )

    async def update_account(self, account_uuid: str, data: AccountUpdate) -> AccountResponse:
        """Частичное обновление; при смене пароля — повторное шифрование в ``password_enc``."""
        key = _account_key(account_uuid)
        current = await self._redis.hgetall(key)
        if not current:
            raise AccountNotFoundError(account_uuid)
        updates: dict[str, str] = {}
        payload = data.model_dump(exclude_unset=True)
        if "username" in payload and payload["username"] is not None:
            updates["username"] = payload["username"]
        if "description" in payload:
            updates["description"] = payload["description"] or ""
        if "password" in payload and payload["password"] is not None:
            updates["password_enc"] = self._crypto.encrypt(payload["password"])
        if updates:
            await self._redis.hset(key, mapping=updates)
        merged = {**current, **updates}
        return _account_to_response(account_uuid, merged)

    async def delete_account(self, account_uuid: str) -> None:
        """Удаляет учётную запись, если ни один хост не ссылается на неё; иначе ``AccountInUseError``."""
        if not await self._redis.exists(_account_key(account_uuid)):
            raise AccountNotFoundError(account_uuid)
        hosts = await self._hosts_using_account(account_uuid)
        if hosts:
            raise AccountInUseError(hosts)
        key = _account_key(account_uuid)
        await self._redis.delete(key)
        await self._redis.srem(ACCOUNTS_REGISTRY_KEY, account_uuid)

    async def get_settings(self) -> GlobalSettings:
        """``HGETALL settings:global``; при отсутствии ключа — запись дефолтов из ТЗ и возврат модели."""
        data = await self._redis.hgetall(SETTINGS_GLOBAL_KEY)
        if not data:
            await self._redis.hset(SETTINGS_GLOBAL_KEY, mapping=_DEFAULT_GLOBAL_HASH)
            return _global_from_hash(_DEFAULT_GLOBAL_HASH)
        return _global_from_hash(data)

    async def update_settings(self, data: GlobalSettingsUpdate) -> GlobalSettings:
        """``HSET`` только переданных полей глобальных настроек."""
        payload = data.model_dump(exclude_unset=True)
        if not payload:
            return await self.get_settings()
        await self.get_settings()
        updates: dict[str, str] = {k: str(v) for k, v in payload.items()}
        await self._redis.hset(SETTINGS_GLOBAL_KEY, mapping=updates)
        merged = await self._redis.hgetall(SETTINGS_GLOBAL_KEY)
        return _global_from_hash(merged)
