"""CRUD для хостов, учётных записей и глобальных настроек в Redis."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field

from mockcontrol.config import settings as app_settings
from mockcontrol.core.crypto import CryptoService
from mockcontrol.core.exceptions import (
    AccountInUseError,
    AccountNotFoundError,
    HostHasMocksError,
    HostNotFoundError,
    MockControlError,
)
from mockcontrol.models.account import AccountCreate, AccountResponse
from mockcontrol.models.host import HostCreate, HostResponse, HostStatus
from mockcontrol.models.settings import GlobalSettings

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HostUpdate(BaseModel):
    """Частичное обновление записи хоста."""

    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    account_uuid: str | None = None
    working_dir: str | None = None
    java_path: str | None = None
    mock_port_min: int | None = Field(default=None, ge=1, le=65535)
    mock_port_max: int | None = Field(default=None, ge=1, le=65535)
    description: str | None = None


class AccountUpdate(BaseModel):
    """Частичное обновление учётной записи."""

    username: str | None = None
    password: str | None = None
    description: str | None = None


class GlobalSettingsUpdate(BaseModel):
    """Частичное обновление глобальных настроек."""

    rate_limit_window_size: int | None = Field(default=None, ge=1)
    host_check_interval_sec: int | None = Field(default=None, ge=1)
    proxy_timeout_sec: int | None = Field(default=None, ge=1)
    log_retention_lines: int | None = Field(default=None, ge=1)


class SettingsService:
    """Операции с `hosts:*`, `accounts:*` и `settings:global`."""

    def __init__(self, redis: Redis, crypto: CryptoService) -> None:
        self._redis = redis
        self._crypto = crypto

    # --- helpers ---

    async def _account_exists(self, account_uuid: str) -> bool:
        return bool(await self._redis.exists(f"accounts:{account_uuid}"))

    def _default_global_mapping(self) -> dict[str, str]:
        return {
            "rate_limit_window_size": str(app_settings.default_rate_limit_window),
            "host_check_interval_sec": str(app_settings.default_host_check_interval),
            "proxy_timeout_sec": str(app_settings.default_proxy_timeout),
            "log_retention_lines": str(app_settings.default_log_retention_lines),
        }

    def _host_response(self, hostname: str, raw: dict[str, str]) -> HostResponse:
        if not raw:
            raise HostNotFoundError(f"Хост «{hostname}» не найден")
        payload = {
            "hostname": hostname,
            "ssh_port": int(raw["ssh_port"]),
            "account_uuid": raw["account_uuid"],
            "working_dir": raw["working_dir"],
            "java_path": raw["java_path"],
            "mock_port_min": int(raw["mock_port_min"]),
            "mock_port_max": int(raw["mock_port_max"]),
            "description": raw.get("description", ""),
            "status": HostStatus(raw.get("status", HostStatus.UNKNOWN.value)),
            "last_checked_at": raw.get("last_checked_at", ""),
        }
        return HostResponse.model_validate(payload)

    def _account_response(self, uuid: str, raw: dict[str, str]) -> AccountResponse:
        if not raw:
            raise AccountNotFoundError(f"Учётная запись «{uuid}» не найдена")
        return AccountResponse(
            uuid=uuid,
            username=raw["username"],
            description=raw.get("description", ""),
            created_at=raw.get("created_at", ""),
        )

    def _global_settings_from_hash(self, raw: dict[str, str]) -> GlobalSettings:
        return GlobalSettings(
            rate_limit_window_size=int(raw["rate_limit_window_size"]),
            host_check_interval_sec=int(raw["host_check_interval_sec"]),
            proxy_timeout_sec=int(raw["proxy_timeout_sec"]),
            log_retention_lines=int(raw["log_retention_lines"]),
        )

    async def _mocks_on_host(self, hostname: str) -> list[str]:
        names = sorted(await self._redis.smembers("mocks:registry"))
        if not names:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for m in names:
            pipe.hgetall(f"mocks:{m}")
        rows = await pipe.execute()
        attached: list[str] = []
        for name, row in zip(names, rows, strict=True):
            row = dict(row) if row else {}
            if (row.get("hostname") or "") == hostname:
                attached.append(name)
        return attached

    # --- hosts ---

    async def list_hosts(self) -> list[HostResponse]:
        hostnames = sorted(await self._redis.smembers("hosts:registry"))
        if not hostnames:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for hn in hostnames:
            pipe.hgetall(f"hosts:{hn}")
        rows = await pipe.execute()
        out: list[HostResponse] = []
        for hn, row in zip(hostnames, rows, strict=True):
            row = dict(row) if row else {}
            if not row:
                logger.warning("hosts:registry содержит «%s», но hash пуст", hn)
                continue
            out.append(self._host_response(hn, row))
        return out

    async def get_host(self, hostname: str) -> HostResponse:
        raw = await self._redis.hgetall(f"hosts:{hostname}")
        return self._host_response(hostname, dict(raw) if raw else {})

    async def create_host(self, data: HostCreate) -> HostResponse:
        hn = data.hostname.strip()
        if not hn:
            raise MockControlError("Пустое имя хоста")

        exists = await self._redis.sismember("hosts:registry", hn)
        if exists:
            raise MockControlError(f"Хост «{hn}» уже существует")

        if not await self._account_exists(data.account_uuid):
            raise AccountNotFoundError(
                f"Учётная запись «{data.account_uuid}» не найдена"
            )

        now = _utc_iso()
        mapping: dict[str, str] = {
            "ssh_port": str(data.ssh_port),
            "account_uuid": data.account_uuid,
            "working_dir": data.working_dir,
            "java_path": data.java_path,
            "mock_port_min": str(data.mock_port_min),
            "mock_port_max": str(data.mock_port_max),
            "description": data.description,
            "status": HostStatus.UNKNOWN.value,
            "last_checked_at": now,
        }
        await self._redis.hset(f"hosts:{hn}", mapping=mapping)
        await self._redis.sadd("hosts:registry", hn)
        return self._host_response(hn, mapping)

    async def update_host(self, hostname: str, data: HostUpdate) -> HostResponse:
        hn = hostname.strip()
        key = f"hosts:{hn}"
        raw = dict(await self._redis.hgetall(key))
        if not raw:
            raise HostNotFoundError(f"Хост «{hn}» не найден")

        updates = data.model_dump(exclude_unset=True)
        if not updates:
            return self._host_response(hn, raw)

        new_account = updates.get("account_uuid")
        if new_account is not None and new_account != raw.get("account_uuid"):
            if not await self._account_exists(new_account):
                raise AccountNotFoundError(f"Учётная запись «{new_account}» не найдена")

        mapping: dict[str, str] = {}
        if "ssh_port" in updates:
            mapping["ssh_port"] = str(updates["ssh_port"])
        if "account_uuid" in updates:
            mapping["account_uuid"] = updates["account_uuid"]
        if "working_dir" in updates:
            mapping["working_dir"] = updates["working_dir"]
        if "java_path" in updates:
            mapping["java_path"] = updates["java_path"]
        if "mock_port_min" in updates:
            mapping["mock_port_min"] = str(updates["mock_port_min"])
        if "mock_port_max" in updates:
            mapping["mock_port_max"] = str(updates["mock_port_max"])
        if "description" in updates:
            mapping["description"] = updates["description"]

        await self._redis.hset(key, mapping=mapping)
        merged = {**raw, **mapping}
        return self._host_response(hn, merged)

    async def delete_host(self, hostname: str) -> None:
        hn = hostname.strip()
        raw = await self._redis.hgetall(f"hosts:{hn}")
        if not raw:
            raise HostNotFoundError(f"Хост «{hn}» не найден")

        attached = await self._mocks_on_host(hn)
        if attached:
            listed = ", ".join(sorted(attached))
            raise HostHasMocksError(
                f"На хосте «{hn}» зарегистрированы заглушки: {listed}"
            )

        await self._redis.delete(f"hosts:{hn}")
        await self._redis.srem("hosts:registry", hn)

    # --- accounts ---

    async def list_accounts(self) -> list[AccountResponse]:
        uuids = sorted(await self._redis.smembers("accounts:registry"))
        if not uuids:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for u in uuids:
            pipe.hgetall(f"accounts:{u}")
        rows = await pipe.execute()
        out: list[AccountResponse] = []
        for uid, row in zip(uuids, rows, strict=True):
            row = dict(row) if row else {}
            if not row:
                logger.warning("accounts:registry содержит «%s», но hash пуст", uid)
                continue
            out.append(self._account_response(uid, row))
        return out

    async def get_account(self, uuid: str) -> AccountResponse:
        raw = dict(await self._redis.hgetall(f"accounts:{uuid}"))
        return self._account_response(uuid, raw)

    async def create_account(self, data: AccountCreate) -> AccountResponse:
        uid = str(uuid4())
        now = _utc_iso()
        password_enc = self._crypto.encrypt(data.password)
        mapping: dict[str, str] = {
            "username": data.username,
            "password_enc": password_enc,
            "description": data.description,
            "created_at": now,
        }
        await self._redis.hset(f"accounts:{uid}", mapping=mapping)
        await self._redis.sadd("accounts:registry", uid)
        return AccountResponse(
            uuid=uid,
            username=data.username,
            description=data.description,
            created_at=now,
        )

    async def update_account(self, uuid: str, data: AccountUpdate) -> AccountResponse:
        key = f"accounts:{uuid}"
        raw = dict(await self._redis.hgetall(key))
        if not raw:
            raise AccountNotFoundError(f"Учётная запись «{uuid}» не найдена")

        updates = data.model_dump(exclude_unset=True)
        if not updates:
            return self._account_response(uuid, raw)

        mapping: dict[str, str] = {}
        if "username" in updates:
            mapping["username"] = updates["username"]
        if "description" in updates:
            mapping["description"] = updates["description"]
        if "password" in updates:
            mapping["password_enc"] = self._crypto.encrypt(updates["password"])

        await self._redis.hset(key, mapping=mapping)
        merged = {**raw, **mapping}
        return self._account_response(uuid, merged)

    async def delete_account(self, uuid: str) -> None:
        key = f"accounts:{uuid}"
        raw = await self._redis.hgetall(key)
        if not raw:
            raise AccountNotFoundError(f"Учётная запись «{uuid}» не найдена")

        hostnames = sorted(await self._redis.smembers("hosts:registry"))
        if hostnames:
            pipe = self._redis.pipeline(transaction=False)
            for hn in hostnames:
                pipe.hgetall(f"hosts:{hn}")
            rows = await pipe.execute()
            using = [
                hn
                for hn, row in zip(hostnames, rows, strict=True)
                if (dict(row) if row else {}).get("account_uuid") == uuid
            ]
            if using:
                listed = ", ".join(sorted(using))
                raise AccountInUseError(
                    f"Учётная запись «{uuid}» используется хостами: {listed}"
                )

        await self._redis.delete(key)
        await self._redis.srem("accounts:registry", uuid)

    # --- global settings ---

    async def get_settings(self) -> GlobalSettings:
        raw = dict(await self._redis.hgetall("settings:global"))
        if not raw:
            defaults = self._default_global_mapping()
            await self._redis.hset("settings:global", mapping=defaults)
            raw = defaults
        return self._global_settings_from_hash(raw)

    async def update_settings(self, data: GlobalSettingsUpdate) -> GlobalSettings:
        updates = data.model_dump(exclude_unset=True)
        if not updates:
            return await self.get_settings()

        await self.get_settings()
        mapping = {k: str(v) for k, v in updates.items()}
        await self._redis.hset("settings:global", mapping=mapping)
        merged_raw = dict(await self._redis.hgetall("settings:global"))
        return self._global_settings_from_hash(merged_raw)
