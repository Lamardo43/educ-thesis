"""
Центральный менеджер жизненного цикла заглушек: регистрация, запуск, остановка,
удаление и восстановление состояния после перезапуска.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from uuid import uuid4

from starlette.datastructures import UploadFile

from mockcontrol.config import settings
from mockcontrol.core.crypto import CryptoService
from mockcontrol.core.exceptions import (
    AccountNotFoundError,
    ArtifactOperationError,
    DecryptionError,
    HostNotFoundError,
    MockAlreadyExistsError,
    MockAlreadyRunningError,
    MockControlError,
    MockNotFoundError,
    MockNotRunningError,
    MockProcessError,
    PortAllocationError,
    SCPError,
    SSHConnectionError,
)
from mockcontrol.core.ssh_pool import SSHPool
from mockcontrol.services.proxy import ProxyClientRegistry

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_SS_PORT_RE = re.compile(r":(\d+)(?:\s|$)")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_path(filename: str) -> str:
    return f"/tmp/mockcontrol_{filename}.log"


def _parse_listen_ports_ss(output: str) -> set[int]:
    ports: set[int] = set()
    for m in _SS_PORT_RE.finditer(output):
        try:
            ports.add(int(m.group(1)))
        except ValueError:
            continue
    return ports


class LifecycleManager:
    """
    Оркестрация заглушек: Redis, локальные вызовы или SSH/SCP через пул,
    расшифровка паролей, httpx-клиенты в ProxyClientRegistry.
    """

    def __init__(
        self,
        redis: Redis,
        ssh_pool: SSHPool,
        crypto: CryptoService,
        proxy_registry: ProxyClientRegistry,
        host_resolver: ModuleType,
    ) -> None:
        self._redis = redis
        self._ssh_pool = ssh_pool
        self._crypto = crypto
        self._proxy = proxy_registry
        self._host_resolver = host_resolver

    def _is_local(self, hostname: str) -> bool:
        return bool(self._host_resolver.is_local(hostname))

    async def _hgetall(self, key: str) -> dict[str, str]:
        raw = await self._redis.hgetall(key)
        return dict(raw) if raw else {}

    async def _load_host(self, hostname: str) -> dict[str, str]:
        data = await self._hgetall(f"hosts:{hostname}")
        if not data:
            raise HostNotFoundError(f"Хост «{hostname}» не найден в Redis")
        return data

    async def _load_account(self, account_uuid: str) -> dict[str, str]:
        data = await self._hgetall(f"accounts:{account_uuid}")
        if not data:
            raise AccountNotFoundError(f"Учётная запись «{account_uuid}» не найдена")
        return data

    async def _load_mock(self, filename: str) -> dict[str, str]:
        data = await self._hgetall(f"mocks:{filename}")
        if not data:
            raise MockNotFoundError(f"Заглушка «{filename}» не найдена")
        return data

    def _int_field(self, d: dict[str, str], key: str, default: int = 0) -> int:
        v = d.get(key)
        if v is None or v == "":
            return default
        try:
            return int(v)
        except ValueError as exc:
            raise MockControlError(f"Некорректное целое поле «{key}»") from exc

    async def _find_free_port_local(self, port_min: int, port_max: int) -> int:
        def try_bind(port: int) -> bool:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                return True
            except OSError:
                return False
            finally:
                s.close()

        for p in range(port_min, port_max + 1):
            ok = await asyncio.to_thread(try_bind, p)
            if ok:
                return p
        raise PortAllocationError(
            f"Нет свободного порта в диапазоне {port_min}–{port_max} на локальном хосте"
        )

    async def _find_free_port_remote(
        self,
        hostname: str,
        ssh_port: int,
        username: str,
        password: str,
        port_min: int,
        port_max: int,
    ) -> int:
        try:
            proc = await self._ssh_pool.run_command(
                hostname,
                ssh_port,
                username,
                password,
                "ss -tlnp 2>/dev/null || ss -tl",
                check=False,
                timeout=30.0,
            )
        except SSHConnectionError:
            raise
        except Exception as exc:
            raise SSHConnectionError(f"Не удалось получить список портов по SSH: {exc}") from exc

        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        used = _parse_listen_ports_ss(out)
        for p in range(port_min, port_max + 1):
            if p not in used:
                return p
        raise PortAllocationError(
            f"Нет свободного порта в диапазоне {port_min}–{port_max} на {hostname}"
        )

    def _jvm_argv(self, jvm_args: str) -> list[str]:
        posix = os.name != "nt"
        return shlex.split((jvm_args or "").strip(), posix=posix)

    def _process_alive_local(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        else:
            return True

    async def _process_alive_remote(
        self,
        hostname: str,
        ssh_port: int,
        username: str,
        password: str,
        pid: int,
    ) -> bool:
        try:
            proc = await self._ssh_pool.run_command(
                hostname,
                ssh_port,
                username,
                password,
                f"kill -0 {int(pid)} 2>/dev/null && echo ok || echo no",
                check=False,
                timeout=15.0,
            )
        except SSHConnectionError:
            return False
        out = (proc.stdout or "").strip()
        return out == "ok"

    async def register_mock(
        self,
        file: UploadFile,
        hostname: str,
        jvm_args: str = "",
        rate_limit: int = 0,
        auto_start: bool = False,
    ) -> None:
        raw_name = (file.filename or "").strip()
        filename = Path(raw_name).name
        if not filename or filename != raw_name or ".." in filename or "/" in raw_name or "\\" in raw_name:
            raise MockControlError("Некорректное или небезопасное имя файла артефакта")

        exists = await self._redis.sismember("mocks:registry", filename)
        if exists:
            raise MockAlreadyExistsError(f"Заглушка с именем файла «{filename}» уже зарегистрирована")

        host = await self._load_host(hostname)
        working_dir = (host.get("working_dir") or "").strip().rstrip("/")
        if not working_dir:
            raise MockControlError("У хоста не задан working_dir")

        artifact_path = f"{working_dir}/{filename}"
        temp_root = Path(settings.temp_upload_dir)
        try:
            await asyncio.to_thread(temp_root.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise ArtifactOperationError(f"Не удалось создать каталог временных загрузок: {exc}") from exc

        temp_path = temp_root / f".upload.{uuid4().hex}.{filename}"
        try:
            body = await file.read()
            await asyncio.to_thread(temp_path.write_bytes, body)
        except OSError as exc:
            raise ArtifactOperationError(f"Не удалось сохранить загруженный файл: {exc}") from exc
        finally:
            await file.close()

        try:
            if self._is_local(hostname):
                dest = Path(artifact_path)
                try:
                    await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.copyfile, str(temp_path), str(dest))
                    await asyncio.to_thread(os.chmod, dest, 0o755)
                except OSError as exc:
                    raise ArtifactOperationError(
                        f"Не удалось скопировать артефакт в {artifact_path}: {exc}"
                    ) from exc
            else:
                ssh_port = self._int_field(host, "ssh_port", 22)
                account_uuid = host.get("account_uuid") or ""
                account = await self._load_account(account_uuid)
                username = account.get("username") or ""
                password_enc = account.get("password_enc") or ""
                if not username:
                    raise MockControlError("В учётной записи не задан username")
                password = self._crypto.decrypt(password_enc)
                mkdir_cmd = f"mkdir -p {shlex.quote(working_dir)}"
                try:
                    await self._ssh_pool.run_command(
                        hostname,
                        ssh_port,
                        username,
                        password,
                        mkdir_cmd,
                        check=True,
                        timeout=60.0,
                    )
                    await self._ssh_pool.scp_upload(
                        hostname,
                        ssh_port,
                        username,
                        password,
                        str(temp_path),
                        artifact_path,
                        timeout=600.0,
                    )
                    chmod_cmd = f"chmod 755 {shlex.quote(artifact_path)}"
                    await self._ssh_pool.run_command(
                        hostname,
                        ssh_port,
                        username,
                        password,
                        chmod_cmd,
                        check=False,
                        timeout=30.0,
                    )
                except SCPError:
                    raise
                except SSHConnectionError:
                    raise
                except Exception as exc:
                    raise ArtifactOperationError(f"Ошибка доставки артефакта по SSH/SCP: {exc}") from exc
        finally:
            try:
                await asyncio.to_thread(lambda: temp_path.unlink(missing_ok=True))
            except OSError as exc:
                logger.warning("Не удалось удалить временный файл %s: %s", temp_path, exc)

        now = _utc_iso()
        mock_key = f"mocks:{filename}"
        mapping: dict[str, str] = {
            "hostname": hostname,
            "jvm_args": jvm_args,
            "rate_limit": str(rate_limit),
            "rate_limit_enabled": "false",
            "status": "REGISTERED",
            "registered_at": now,
            "artifact_path": artifact_path,
        }
        await self._redis.hset(mock_key, mapping=mapping)
        await self._redis.sadd("mocks:registry", filename)

        if auto_start:
            await self.start_mock(filename)

    async def start_mock(self, filename: str) -> None:
        mock = await self._load_mock(filename)
        status = (mock.get("status") or "").upper()
        if status == "RUNNING":
            raise MockAlreadyRunningError(f"Заглушка «{filename}» уже запущена")

        hostname = mock.get("hostname") or ""
        if not hostname:
            raise MockControlError("В записи заглушки не указан hostname")

        host = await self._load_host(hostname)
        port_min = self._int_field(host, "mock_port_min")
        port_max = self._int_field(host, "mock_port_max")
        if port_min > port_max:
            raise MockControlError("mock_port_min больше mock_port_max в конфигурации хоста")

        java_path = (host.get("java_path") or "").strip()
        if not java_path:
            raise MockControlError("У хоста не задан java_path")

        artifact_path = (mock.get("artifact_path") or "").strip()
        if not artifact_path:
            raise MockControlError("В записи заглушки отсутствует artifact_path")

        jvm_args = mock.get("jvm_args") or ""

        if self._is_local(hostname):
            port = await self._find_free_port_local(port_min, port_max)
            log_file = _log_path(filename)
            jvm_argv = self._jvm_argv(jvm_args)
            log_fp = None
            try:
                log_fp = open(log_file, "ab", buffering=0)
                start_sess = os.name != "nt"
                try:
                    proc = await asyncio.create_subprocess_exec(
                        java_path,
                        *jvm_argv,
                        "-jar",
                        artifact_path,
                        f"--server.port={port}",
                        stdout=log_fp,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=start_sess,
                    )
                except OSError as exc:
                    raise MockProcessError(f"Не удалось запустить Java-процесс: {exc}") from exc
                pid = proc.pid
            finally:
                if log_fp is not None:
                    log_fp.close()
        else:
            ssh_port = self._int_field(host, "ssh_port", 22)
            account_uuid = host.get("account_uuid") or ""
            account = await self._load_account(account_uuid)
            username = account.get("username") or ""
            password = self._crypto.decrypt(account.get("password_enc") or "")
            port = await self._find_free_port_remote(
                hostname, ssh_port, username, password, port_min, port_max
            )
            log_remote = _log_path(filename)
            inner = " ".join(
                [
                    shlex.quote(java_path),
                    jvm_args.strip(),
                    "-jar",
                    shlex.quote(artifact_path),
                    f"--server.port={port}",
                ]
            ).strip()
            cmd = f"nohup {inner} > {shlex.quote(log_remote)} 2>&1 & echo $!"
            try:
                proc = await self._ssh_pool.run_command(
                    hostname,
                    ssh_port,
                    username,
                    password,
                    cmd,
                    check=True,
                    timeout=60.0,
                )
            except SSHConnectionError:
                raise
            except Exception as exc:
                raise MockProcessError(f"Ошибка удалённого запуска Java: {exc}") from exc
            out = (proc.stdout or "").strip().splitlines()
            if not out:
                raise MockProcessError("Пустой ответ SSH при запуске процесса (нет PID)")
            try:
                pid = int(out[-1].strip())
            except ValueError as exc:
                raise MockProcessError(f"Не удалось разобрать PID из ответа SSH: {out!r}") from exc

        await asyncio.sleep(2.0)
        if self._is_local(hostname):
            alive = self._process_alive_local(pid)
        else:
            ssh_port = self._int_field(host, "ssh_port", 22)
            account_uuid = host.get("account_uuid") or ""
            account = await self._load_account(account_uuid)
            username = account.get("username") or ""
            password = self._crypto.decrypt(account.get("password_enc") or "")
            alive = await self._process_alive_remote(hostname, ssh_port, username, password, pid)

        if not alive:
            raise MockProcessError(f"Процесс заглушки «{filename}» (PID {pid}) не подтвердился после запуска")

        started_at = _utc_iso()
        mock_key = f"mocks:{filename}"
        await self._redis.hset(
            mock_key,
            mapping={
                "port": str(port),
                "pid": str(pid),
                "status": "RUNNING",
                "started_at": started_at,
            },
        )

        timeout_sec = float(settings.default_proxy_timeout)
        base_url = f"http://{hostname}:{port}"
        await self._proxy.get_or_create(filename, base_url, timeout_sec)

    async def stop_mock(self, filename: str) -> None:
        mock = await self._load_mock(filename)
        status = (mock.get("status") or "").upper()
        pid_raw = mock.get("pid")
        hostname = mock.get("hostname") or ""

        if status != "RUNNING" or not pid_raw:
            raise MockNotRunningError(f"Заглушка «{filename}» не в состоянии RUNNING или PID отсутствует")

        try:
            pid = int(pid_raw)
        except ValueError as exc:
            raise MockNotRunningError(f"Некорректный PID в Redis для «{filename}»") from exc

        if self._is_local(hostname):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise MockProcessError(f"Нет прав для SIGTERM процессу {pid}: {exc}") from exc
        else:
            host = await self._load_host(hostname)
            ssh_port = self._int_field(host, "ssh_port", 22)
            account = await self._load_account(host.get("account_uuid") or "")
            username = account.get("username") or ""
            password = self._crypto.decrypt(account.get("password_enc") or "")
            try:
                await self._ssh_pool.run_command(
                    hostname,
                    ssh_port,
                    username,
                    password,
                    f"kill -TERM {pid} 2>/dev/null || true",
                    check=False,
                    timeout=30.0,
                )
            except SSHConnectionError:
                raise

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._is_local(hostname):
                alive = self._process_alive_local(pid)
            else:
                host = await self._load_host(hostname)
                ssh_port = self._int_field(host, "ssh_port", 22)
                account = await self._load_account(host.get("account_uuid") or "")
                username = account.get("username") or ""
                password = self._crypto.decrypt(account.get("password_enc") or "")
                alive = await self._process_alive_remote(hostname, ssh_port, username, password, pid)
            if not alive:
                break
            await asyncio.sleep(0.15)

        if self._is_local(hostname):
            if self._process_alive_local(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            host = await self._load_host(hostname)
            ssh_port = self._int_field(host, "ssh_port", 22)
            account = await self._load_account(host.get("account_uuid") or "")
            username = account.get("username") or ""
            password = self._crypto.decrypt(account.get("password_enc") or "")
            if await self._process_alive_remote(hostname, ssh_port, username, password, pid):
                await self._ssh_pool.run_command(
                    hostname,
                    ssh_port,
                    username,
                    password,
                    f"kill -KILL {pid} 2>/dev/null || true",
                    check=False,
                    timeout=30.0,
                )

        mock_key = f"mocks:{filename}"
        await self._redis.hset(mock_key, mapping={"status": "STOPPED"})
        await self._redis.hdel(mock_key, "port", "pid", "started_at")
        await self._proxy.remove(filename)

    async def delete_mock(self, filename: str) -> None:
        mock = await self._load_mock(filename)
        if (mock.get("status") or "").upper() == "RUNNING":
            try:
                await self.stop_mock(filename)
            except MockNotRunningError:
                pass
            mock = await self._hgetall(f"mocks:{filename}")

        hostname = mock.get("hostname") or ""
        artifact_path = (mock.get("artifact_path") or "").strip()

        if artifact_path:
            if self._is_local(hostname):
                try:
                    await asyncio.to_thread(lambda p=artifact_path: Path(p).unlink(missing_ok=True))
                except OSError as exc:
                    raise ArtifactOperationError(f"Не удалось удалить артефакт: {exc}") from exc
            else:
                host = await self._load_host(hostname)
                ssh_port = self._int_field(host, "ssh_port", 22)
                account = await self._load_account(host.get("account_uuid") or "")
                username = account.get("username") or ""
                password = self._crypto.decrypt(account.get("password_enc") or "")
                try:
                    await self._ssh_pool.run_command(
                        hostname,
                        ssh_port,
                        username,
                        password,
                        f"rm -f {shlex.quote(artifact_path)}",
                        check=False,
                        timeout=60.0,
                    )
                except SSHConnectionError:
                    raise

        await self._redis.delete(f"mocks:{filename}")
        await self._redis.srem("mocks:registry", filename)
        await self._redis.delete(f"logs:{filename}")

        async for key in self._redis.scan_iter(match=f"rate:{filename}:*"):
            await self._redis.delete(key)

        await self._redis.delete(
            f"metrics:proxy_total:{filename}",
            f"metrics:rejected_total:{filename}",
        )
        await self._proxy.remove(filename)

    async def restore_state(self) -> dict[str, str]:
        """
        Для записей со статусом RUNNING проверяет процесс на хосте.
        Возвращает словарь {filename: новый_статус} для изменённых записей.
        """
        changes: dict[str, str] = {}
        members = await self._redis.smembers("mocks:registry")
        for filename in members:
            data = await self._hgetall(f"mocks:{filename}")
            if (data.get("status") or "").upper() != "RUNNING":
                continue
            pid_raw = data.get("pid")
            hostname = data.get("hostname") or ""
            if not pid_raw or not hostname:
                await self._redis.hset(f"mocks:{filename}", mapping={"status": "STOPPED"})
                await self._redis.hdel(f"mocks:{filename}", "port", "pid", "started_at")
                await self._proxy.remove(filename)
                changes[filename] = "STOPPED"
                continue
            try:
                pid = int(pid_raw)
            except ValueError:
                await self._redis.hset(f"mocks:{filename}", mapping={"status": "STOPPED"})
                await self._redis.hdel(f"mocks:{filename}", "port", "pid", "started_at")
                await self._proxy.remove(filename)
                changes[filename] = "STOPPED"
                continue

            alive: bool
            try:
                if self._is_local(hostname):
                    alive = self._process_alive_local(pid)
                else:
                    host = await self._load_host(hostname)
                    ssh_port = self._int_field(host, "ssh_port", 22)
                    account = await self._load_account(host.get("account_uuid") or "")
                    username = account.get("username") or ""
                    password = self._crypto.decrypt(account.get("password_enc") or "")
                    alive = await self._process_alive_remote(
                        hostname, ssh_port, username, password, pid
                    )
            except (
                HostNotFoundError,
                AccountNotFoundError,
                SSHConnectionError,
                DecryptionError,
            ) as exc:
                logger.warning("restore_state: не удалось проверить %s: %s", filename, exc)
                alive = False

            if alive:
                port = self._int_field(data, "port", 0)
                if port > 0:
                    timeout_sec = float(settings.default_proxy_timeout)
                    base_url = f"http://{hostname}:{port}"
                    await self._proxy.get_or_create(filename, base_url, timeout_sec)
            else:
                await self._redis.hset(f"mocks:{filename}", mapping={"status": "STOPPED"})
                await self._redis.hdel(f"mocks:{filename}", "port", "pid", "started_at")
                await self._proxy.remove(filename)
                changes[filename] = "STOPPED"

        return changes
