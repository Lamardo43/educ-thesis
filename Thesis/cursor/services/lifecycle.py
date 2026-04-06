"""Lifecycle Manager — регистрация, запуск, остановка и удаление заглушек (ТЗ, раздел «Lifecycle Manager»).

Все операции с целевым хостом ветвятся через ``host_resolver.is_local(hostname)``: локально —
прямые вызовы ОС и ``asyncio.create_subprocess_exec``; удалённо — SSH/SFTP из пула.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from redis.asyncio import Redis
from starlette.datastructures import UploadFile

from config import Settings
from core.exceptions import (
    AccountNotFoundError,
    HostNotFoundError,
    InvalidMockFileError,
    LifecycleOperationError,
    MockAlreadyExistsError,
    MockAlreadyRunningError,
    MockNotFoundError,
    MockNotRunningError,
    SCPError,
    SSHConnectionError,
)
from core.crypto import CryptoService
from core.ssh_pool import SSHPool
from services.proxy import ProxyClientRegistry

logger = logging.getLogger(__name__)

MOCKS_REGISTRY_KEY = "mocks:registry"
SETTINGS_GLOBAL_KEY = "settings:global"


class _HostResolver(Protocol):
    """Модуль или объект с функцией ``is_local(hostname)`` (см. ``core.host_resolver``)."""

    is_local: Any  # Callable[[str], bool]


def _mock_key(filename: str) -> str:
    return f"mocks:{filename}"


def _host_key(hostname: str) -> str:
    return f"hosts:{hostname}"


def _account_key(account_uuid: str) -> str:
    return f"accounts:{account_uuid}"


def _log_path_for_filename(filename: str) -> str:
    safe = filename.replace("/", "_").replace("\x00", "")
    return f"/tmp/mockcontrol_{safe}.log"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ssh_cmd_succeeded(proc: Any) -> bool:
    """Проверка успешного завершения команды asyncssh (``exit_status`` / ``returncode``)."""
    es = getattr(proc, "exit_status", None)
    if es is not None:
        return int(es) == 0
    rc = getattr(proc, "returncode", None)
    if rc is not None:
        return int(rc) == 0
    return False


def _parse_ss_listening_ports(stdout: str) -> set[int]:
    ports: set[int] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("State"):
            continue
        for part in line.split():
            if ":" not in part:
                continue
            _addr, _sep, port_s = part.rpartition(":")
            if port_s.isdigit():
                ports.add(int(port_s))
    return ports


class LifecycleManager:
    """Центральное управление жизненным циклом Java-заглушек."""

    def __init__(
        self,
        redis: Redis,
        ssh_pool: SSHPool,
        crypto: CryptoService,
        proxy_clients: ProxyClientRegistry,
        host_resolver: _HostResolver,
    ) -> None:
        self._redis = redis
        self._ssh = ssh_pool
        self._crypto = crypto
        self._proxy_clients = proxy_clients
        self._host_resolver = host_resolver
        self._settings = Settings()

    def _is_local(self, hostname: str) -> bool:
        return bool(self._host_resolver.is_local(hostname))

    async def _proxy_timeout_sec(self) -> float:
        raw = await self._redis.hget(SETTINGS_GLOBAL_KEY, "proxy_timeout_sec")
        if raw is None or raw == "":
            return float(self._settings.default_proxy_timeout)
        try:
            return float(raw)
        except ValueError:
            return float(self._settings.default_proxy_timeout)

    async def _load_host(self, hostname: str) -> dict[str, str]:
        data = await self._redis.hgetall(_host_key(hostname))
        if not data:
            raise HostNotFoundError(f"Хост не найден: {hostname!r}")
        return data

    async def _load_account(self, account_uuid: str) -> dict[str, str]:
        data = await self._redis.hgetall(_account_key(account_uuid))
        if not data:
            raise AccountNotFoundError(f"Учётная запись не найдена: {account_uuid!r}")
        return data

    async def _load_mock(self, filename: str) -> dict[str, str]:
        data = await self._redis.hgetall(_mock_key(filename))
        if not data:
            raise MockNotFoundError(f"Заглушка не найдена: {filename!r}")
        return data

    async def _ssh_creds_for_host(self, host: dict[str, str], hostname: str) -> tuple[str, str, int]:
        account_uuid = host.get("account_uuid", "")
        if not account_uuid:
            raise LifecycleOperationError("У хоста не задана учётная запись (account_uuid)")
        acc = await self._load_account(account_uuid)
        enc = acc.get("password_enc", "")
        username = acc.get("username", "")
        if not username or not enc:
            raise AccountNotFoundError("Неполные данные учётной записи SSH")
        try:
            password = self._crypto.decrypt(enc)
        except ValueError as exc:
            raise SSHConnectionError("Не удалось расшифровать пароль учётной записи SSH") from exc
        try:
            port = int(host.get("ssh_port", "22"))
        except ValueError as exc:
            raise LifecycleOperationError("Некорректный ssh_port у хоста") from exc
        return username, password, port

    async def _pick_free_port_local(self, pmin: int, pmax: int) -> int:
        if pmin > pmax:
            pmin, pmax = pmax, pmin

        def port_bindable(port: int) -> bool:
            import socket

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False
            finally:
                s.close()

        for port in range(pmin, pmax + 1):
            ok = await asyncio.to_thread(port_bindable, port)
            if ok:
                return port
        raise LifecycleOperationError(
            f"Нет свободного TCP-порта в диапазоне [{pmin}, {pmax}] на локальном хосте"
        )

    async def _pick_free_port_remote(
        self,
        hostname: str,
        username: str,
        password: str,
        ssh_port: int,
        pmin: int,
        pmax: int,
    ) -> int:
        if pmin > pmax:
            pmin, pmax = pmax, pmin
        proc = await self._ssh.run_command(
            hostname,
            username,
            password,
            "ss -tln 2>/dev/null || netstat -tln 2>/dev/null",
            port=ssh_port,
            timeout=30.0,
        )
        out = proc.stdout or ""
        occupied = _parse_ss_listening_ports(out)
        for port in range(pmin, pmax + 1):
            if port not in occupied:
                return port
        raise LifecycleOperationError(
            f"Нет свободного TCP-порта в диапазоне [{pmin}, {pmax}] на хосте {hostname!r}"
        )

    async def _process_alive_local(self, pid: int) -> bool:
        def check() -> bool:
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            return True

        return await asyncio.to_thread(check)

    async def _process_alive_remote(
        self,
        hostname: str,
        username: str,
        password: str,
        ssh_port: int,
        pid: int,
    ) -> bool:
        proc = await self._ssh.run_command(
            hostname,
            username,
            password,
            f"kill -0 {int(pid)} 2>/dev/null && echo OK || echo NO",
            port=ssh_port,
            timeout=15.0,
        )
        out = (proc.stdout or "").strip()
        return out.endswith("OK")

    async def register_mock(
        self,
        file: UploadFile,
        hostname: str,
        jvm_args: str,
        rate_limit: int,
        auto_start: bool,
    ) -> None:
        raw_name = (file.filename or "").strip()
        filename = Path(raw_name).name
        if not filename:
            raise InvalidMockFileError("Пустое или некорректное имя файла артефакта")

        if await self._redis.sismember(MOCKS_REGISTRY_KEY, filename):
            raise MockAlreadyExistsError(f"Заглушка уже зарегистрирована: {filename!r}")

        host = await self._load_host(hostname)
        working_dir = (host.get("working_dir") or "").rstrip("/")
        if not working_dir:
            raise LifecycleOperationError("У хоста не задана рабочая директория (working_dir)")
        artifact_path = f"{working_dir}/{filename}"

        upload_root = Path(self._settings.temp_upload_dir)
        await asyncio.to_thread(upload_root.mkdir, parents=True, exist_ok=True)
        temp_name = f"{uuid.uuid4().hex}_{filename}"
        temp_path = upload_root / temp_name

        body = await file.read()
        if not body:
            raise InvalidMockFileError("Пустой файл артефакта")

        await asyncio.to_thread(temp_path.write_bytes, body)

        try:
            if self._is_local(hostname):
                dest = Path(artifact_path)
                await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copyfile, str(temp_path), str(dest))
                await asyncio.to_thread(os.chmod, str(dest), 0o755)
            else:
                user, pwd, ssh_port = await self._ssh_creds_for_host(host, hostname)
                parent = str(Path(artifact_path).parent).replace("\\", "/")
                mkdir_cmd = f"mkdir -p {shlex.quote(parent)}"
                mk = await self._ssh.run_command(hostname, user, pwd, mkdir_cmd, port=ssh_port, timeout=60.0)
                if not _ssh_cmd_succeeded(mk):
                    raise SSHConnectionError(
                        f"Не удалось создать каталог на удалённом хосте: {mkdir_cmd!r}"
                    )
                await self._ssh.scp_upload(hostname, user, pwd, temp_path, artifact_path, port=ssh_port)
                chmod_cmd = f"chmod 755 {shlex.quote(artifact_path)}"
                ch = await self._ssh.run_command(hostname, user, pwd, chmod_cmd, port=ssh_port, timeout=30.0)
                if not _ssh_cmd_succeeded(ch):
                    raise SCPError(f"Не удалось выставить права на {artifact_path!r}")
        except (OSError, shutil.Error) as exc:
            raise LifecycleOperationError(f"Ошибка копирования артефакта: {exc}") from exc
        finally:
            def _unlink_tmp() -> None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

            await asyncio.to_thread(_unlink_tmp)

        registered_at = _utc_iso()
        rate_limit_enabled = "true" if rate_limit > 0 else "false"
        mock_hash = {
            "hostname": hostname,
            "port": "",
            "pid": "",
            "status": "REGISTERED",
            "jvm_args": jvm_args or "",
            "rate_limit": str(rate_limit),
            "rate_limit_enabled": rate_limit_enabled,
            "registered_at": registered_at,
            "started_at": "",
            "artifact_path": artifact_path,
        }
        key = _mock_key(filename)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=mock_hash)
            pipe.sadd(MOCKS_REGISTRY_KEY, filename)
            await pipe.execute()

        logger.info("Mock registered: %s on %s", filename, hostname)

        if auto_start:
            await self.start_mock(filename)

    async def start_mock(self, filename: str) -> None:
        mock = await self._load_mock(filename)
        status = mock.get("status", "")
        if status == "RUNNING":
            raise MockAlreadyRunningError(f"Заглушка уже запущена: {filename!r}")

        hostname = mock.get("hostname", "")
        if not hostname:
            raise LifecycleOperationError("В записи заглушки отсутствует hostname")

        host = await self._load_host(hostname)
        try:
            pmin = int(host.get("mock_port_min", "0"))
            pmax = int(host.get("mock_port_max", "0"))
        except ValueError as exc:
            raise LifecycleOperationError("Некорректные mock_port_min / mock_port_max у хоста") from exc

        java_path = host.get("java_path", "")
        artifact_path = mock.get("artifact_path", "")
        jvm_args = mock.get("jvm_args", "")
        if not java_path or not artifact_path:
            raise LifecycleOperationError("Не заданы java_path или artifact_path")

        if self._is_local(hostname):
            port = await self._pick_free_port_local(pmin, pmax)
            log_file_path = _log_path_for_filename(filename)

            def open_log() -> object:
                return open(log_file_path, "ab")

            lf = await asyncio.to_thread(open_log)
            try:
                argv = [java_path]
                argv.extend(shlex.split(jvm_args) if jvm_args.strip() else [])
                # argv.extend(["-jar", artifact_path, f"--server.port={port}"])
                argv.extend(["-jar", artifact_path, f"{port}"])
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=lf,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                lf.close()
            pid = proc.pid
        else:
            user, pwd, ssh_port = await self._ssh_creds_for_host(host, hostname)
            port = await self._pick_free_port_remote(hostname, user, pwd, ssh_port, pmin, pmax)
            log_remote = _log_path_for_filename(filename)
            jvm_part = (
                " ".join(shlex.quote(t) for t in shlex.split(jvm_args.strip()))
                if jvm_args.strip()
                else ""
            )
            mid = f"{jvm_part} " if jvm_part else ""
            cmd = (
                f"nohup {shlex.quote(java_path)} {mid}"
                # f"-jar {shlex.quote(artifact_path)} --server.port={int(port)} "
                f"-jar {shlex.quote(artifact_path)} {int(port)} "
                f">> {shlex.quote(log_remote)} 2>&1 & echo $!"
            )
            proc = await self._ssh.run_command(hostname, user, pwd, cmd, port=ssh_port, timeout=60.0)
            if not _ssh_cmd_succeeded(proc):
                err = (proc.stderr or proc.stdout or "").strip()
                raise SSHConnectionError(f"Не удалось запустить Java по SSH: {err}")
            out = (proc.stdout or "").strip().splitlines()
            if not out:
                raise SSHConnectionError("Пустой ответ при запуске Java (ожидался PID)")
            try:
                pid = int(out[-1].strip())
            except ValueError as exc:
                raise SSHConnectionError(f"Не удалось разобрать PID процесса: {out!r}") from exc

        await asyncio.sleep(2.0)

        if self._is_local(hostname):
            alive = await self._process_alive_local(pid)
        else:
            user, pwd, ssh_port = await self._ssh_creds_for_host(host, hostname)
            alive = await self._process_alive_remote(hostname, user, pwd, ssh_port, pid)

        if not alive:
            raise LifecycleOperationError("Процесс Java не найден после запуска (завершился или не стартовал)")

        started_at = _utc_iso()
        await self._redis.hset(
            _mock_key(filename),
            mapping={
                "port": str(port),
                "pid": str(pid),
                "status": "RUNNING",
                "started_at": started_at,
            },
        )

        timeout = await self._proxy_timeout_sec()
        base_url = f"http://{hostname}:{port}"
        await self._proxy_clients.get_or_create(filename, base_url, timeout)
        logger.info("Mock started: %s pid=%s port=%s", filename, pid, port)

    async def stop_mock(self, filename: str) -> None:
        mock = await self._load_mock(filename)
        hostname = mock.get("hostname", "")
        pid_s = mock.get("pid", "").strip()
        if not pid_s:
            raise MockNotRunningError(f"Заглушка не запущена (нет PID): {filename!r}")
        try:
            pid = int(pid_s)
        except ValueError as exc:
            raise MockNotRunningError(f"Некорректный PID в Redis: {pid_s!r}") from exc

        if self._is_local(hostname):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise LifecycleOperationError(f"SIGTERM локальному процессу {pid}: {exc}") from exc

            for _ in range(50):
                await asyncio.sleep(0.1)
                if not await self._process_alive_local(pid):
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    raise LifecycleOperationError(f"SIGKILL локальному процессу {pid}: {exc}") from exc
        else:
            host = await self._load_host(hostname)
            user, pwd, ssh_port = await self._ssh_creds_for_host(host, hostname)
            await self._ssh.run_command(
                hostname,
                user,
                pwd,
                f"kill -TERM {pid} 2>/dev/null || true",
                port=ssh_port,
                timeout=15.0,
            )
            for _ in range(50):
                await asyncio.sleep(0.1)
                if not await self._process_alive_remote(hostname, user, pwd, ssh_port, pid):
                    break
            else:
                await self._ssh.run_command(
                    hostname,
                    user,
                    pwd,
                    f"kill -KILL {pid} 2>/dev/null || true",
                    port=ssh_port,
                    timeout=15.0,
                )

        await self._redis.hset(
            _mock_key(filename),
            mapping={
                "status": "STOPPED",
                "port": "",
                "pid": "",
                "started_at": "",
            },
        )
        await self._proxy_clients.remove(filename)
        logger.info("Mock stopped: %s", filename)

    async def delete_mock(self, filename: str) -> None:
        if not await self._redis.sismember(MOCKS_REGISTRY_KEY, filename):
            raise MockNotFoundError(f"Заглушка не в реестре: {filename!r}")

        mock = await self._load_mock(filename)
        if mock.get("status") == "RUNNING":
            try:
                await self.stop_mock(filename)
            except MockNotRunningError:
                pass

        hostname = mock.get("hostname", "")
        artifact_path = mock.get("artifact_path", "")

        if artifact_path:
            if self._is_local(hostname):
                try:

                    def rm() -> None:
                        if os.path.isfile(artifact_path):
                            os.remove(artifact_path)

                    await asyncio.to_thread(rm)
                except OSError as exc:
                    logger.warning("Failed to delete artifact locally: %s", exc)
            else:
                try:
                    host = await self._load_host(hostname)
                    user, pwd, ssh_port = await self._ssh_creds_for_host(host, hostname)
                    await self._ssh.run_command(
                        hostname,
                        user,
                        pwd,
                        f"rm -f {shlex.quote(artifact_path)}",
                        port=ssh_port,
                        timeout=30.0,
                    )
                except (SSHConnectionError, SCPError) as exc:
                    logger.warning("Failed to delete artifact over SSH: %s", exc)

        await self._redis.delete(_mock_key(filename))
        await self._redis.srem(MOCKS_REGISTRY_KEY, filename)
        await self._redis.delete(f"logs:{filename}")
        await self._proxy_clients.remove(filename)

        pattern = f"rate:{filename}:*"
        cursor: int | str = 0
        while True:
            cursor, keys = await self._redis.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                await self._redis.delete(*keys)
            if cursor in (0, "0"):
                break

        await self._redis.delete(f"metrics:proxy_total:{filename}")
        await self._redis.delete(f"metrics:rejected_total:{filename}")
        await self._redis.delete(f"metrics:proxy_stage_sum_ms:{filename}")
        await self._redis.delete(f"metrics:proxy_stage_count:{filename}")
        await self._redis.delete(f"metrics:proxy_stage_last_ms:{filename}")
        await self._redis.delete(f"metrics:proxy_outcome_total:{filename}")
        await self._redis.delete(f"metrics:proxy_upstream_status_total:{filename}")
        logger.info("Mock deleted: %s", filename)

    async def restore_state(self) -> None:
        names = await self._redis.smembers(MOCKS_REGISTRY_KEY)
        for filename in names:
            data = await self._redis.hgetall(_mock_key(filename))
            if data.get("status") != "RUNNING":
                continue
            hostname = data.get("hostname", "")
            pid_s = (data.get("pid") or "").strip()
            port_s = (data.get("port") or "").strip()
            if not pid_s or not port_s:
                await self._redis.hset(
                    _mock_key(filename),
                    mapping={
                        "status": "STOPPED",
                        "port": "",
                        "pid": "",
                        "started_at": "",
                    },
                )
                await self._proxy_clients.remove(filename)
                continue
            try:
                pid = int(pid_s)
                port = int(port_s)
            except ValueError:
                await self._redis.hset(
                    _mock_key(filename),
                    mapping={
                        "status": "STOPPED",
                        "port": "",
                        "pid": "",
                        "started_at": "",
                    },
                )
                await self._proxy_clients.remove(filename)
                continue

            if self._is_local(hostname):
                alive = await self._process_alive_local(pid)
            else:
                try:
                    host = await self._load_host(hostname)
                    user, pwd, ssh_port = await self._ssh_creds_for_host(host, hostname)
                    alive = await self._process_alive_remote(hostname, user, pwd, ssh_port, pid)
                except Exception:
                    logger.exception("restore_state: PID check error for %s", filename)
                    alive = False

            if alive:
                timeout = await self._proxy_timeout_sec()
                base_url = f"http://{hostname}:{port}"
                await self._proxy_clients.get_or_create(filename, base_url, timeout)
                logger.info("restore_state: process alive, proxy client restored: %s", filename)
            else:
                await self._redis.hset(
                    _mock_key(filename),
                    mapping={
                        "status": "STOPPED",
                        "port": "",
                        "pid": "",
                        "started_at": "",
                    },
                )
                await self._proxy_clients.remove(filename)
                logger.warning("restore_state: process dead, status STOPPED: %s", filename)
