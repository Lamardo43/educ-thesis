"""
LifecycleManager — manages the full lifecycle of mock services.

Поддерживает два режима работы, определяемых автоматически по hostname хоста:

  Localhost-режим (hostname == "localhost" или "127.0.0.1"):
    - Копирование файла: shutil.copy (локальная файловая система)
    - Запуск/остановка: subprocess / os.kill
    - Логи: прямое чтение файла

  Удалённый режим (любой другой hostname):
    - Копирование файла: SFTP через paramiko
    - Запуск/остановка: SSH-команды через paramiko
    - Логи: SSH tail
"""
import asyncio
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import redis.asyncio as aioredis

from app.core.crypto import decrypt_password
from app.models.mock import MockRecord, MockStatus
from app.repositories.account_repo import get_account
from app.repositories.host_repo import get_host
from app.repositories.mock_repo import (
    delete_mock,
    get_mock,
    mock_exists,
    save_mock,
)

logger = logging.getLogger(__name__)

_LIVENESS_CHECK_DELAY = 3
_LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1"}


def _is_localhost(hostname: str) -> bool:
    return hostname.strip().lower() in _LOCALHOST_NAMES


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _build_ssh_client(hostname: str, port: int, username: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=hostname,
        port=port,
        username=username,
        password=password,
        timeout=10,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def _ssh_exec(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    _, stdout, stderr = client.exec_command(command)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stdout.read().decode().strip(), stderr.read().decode().strip()


def _sftp_makedirs(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    import stat as stat_mod
    parts = remote_path.replace("\\", "/").split("/")
    current = ""
    for part in parts:
        if not part:
            current = "/"
            continue
        current = f"{current}/{part}".replace("//", "/")
        try:
            st = sftp.stat(current)
            if not stat_mod.S_ISDIR(st.st_mode):
                raise NotADirectoryError(f"Remote path '{current}' exists but is not a directory")
        except FileNotFoundError:
            sftp.mkdir(current)


def _find_free_port_ssh(client: paramiko.SSHClient, port_min: int, port_max: int) -> int:
    code, out, _ = _ssh_exec(
        client,
        "ss -tlnH 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un"
    )
    used_ports = set(int(p) for p in out.splitlines() if p.isdigit())
    for port in range(port_min, port_max + 1):
        if port not in used_ports:
            return port
    raise RuntimeError(f"No free port in range {port_min}-{port_max} on remote host")


async def _get_ssh_credentials(r: aioredis.Redis, hostname: str) -> tuple[str, int, str, str]:
    host = await get_host(r, hostname)
    if host is None:
        raise ValueError(f"Host '{hostname}' not found in registry")
    account = await get_account(r, host.account_uuid)
    if account is None:
        raise ValueError(f"Account '{host.account_uuid}' not found in registry")
    password = decrypt_password(account.password_enc)
    return host.hostname, host.ssh_port, account.username, password


# ---------------------------------------------------------------------------
# Localhost helpers
# ---------------------------------------------------------------------------

def _find_free_port_local(port_min: int, port_max: int) -> int:
    import socket
    for port in range(port_min, port_max + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in range {port_min}-{port_max} on localhost")


# ---------------------------------------------------------------------------
# register_mock
# ---------------------------------------------------------------------------

async def register_mock(
    r: aioredis.Redis,
    filename: str,
    file_bytes: bytes,
    hostname: str,
    jvm_args: str,
    port_arg_template: str,
    rate_limit: int,
    start_immediately: bool,
) -> MockRecord:
    if await mock_exists(r, filename):
        raise ValueError(f"Mock '{filename}' already registered. Delete it first or rename the file.")

    host = await get_host(r, hostname)
    if host is None:
        raise ValueError(f"Host '{hostname}' not found in registry")

    if _is_localhost(hostname):
        def _do_copy() -> None:
            dest_dir = Path(host.working_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / filename).write_bytes(file_bytes)
            logger.info("Copied artifact %s -> %s", filename, dest_dir)
        await asyncio.to_thread(_do_copy)
    else:
        hostname_addr, ssh_port, username, password = await _get_ssh_credentials(r, hostname)
        def _do_scp() -> None:
            import io
            client = _build_ssh_client(hostname_addr, ssh_port, username, password)
            try:
                with client.open_sftp() as sftp:
                    _sftp_makedirs(sftp, host.working_dir)
                    sftp.putfo(io.BytesIO(file_bytes), f"{host.working_dir}/{filename}")
                logger.info("SCP uploaded %s -> %s:%s", filename, hostname, host.working_dir)
            finally:
                client.close()
        await asyncio.to_thread(_do_scp)

    mock = MockRecord(
        filename=filename,
        hostname=hostname,
        jvm_args=jvm_args,
        port_arg_template=port_arg_template,
        rate_limit=rate_limit,
        rate_limit_enabled=False,
        registered_at=datetime.now(timezone.utc),
        status=MockStatus.REGISTERED,
    )
    await save_mock(r, mock)

    if start_immediately:
        mock = await start_mock(r, filename)

    return mock


# ---------------------------------------------------------------------------
# start_mock
# ---------------------------------------------------------------------------

async def start_mock(r: aioredis.Redis, filename: str) -> MockRecord:
    mock = await get_mock(r, filename)
    if mock is None:
        raise ValueError(f"Mock '{filename}' not found")

    host = await get_host(r, mock.hostname)
    if host is None:
        raise ValueError(f"Host '{mock.hostname}' not found")

    if _is_localhost(mock.hostname):
        pid, port = await asyncio.to_thread(_start_local, mock, host)
    else:
        hostname_addr, ssh_port, username, password = await _get_ssh_credentials(r, mock.hostname)
        pid, port = await asyncio.to_thread(
            _start_remote, mock, host, hostname_addr, ssh_port, username, password
        )

    mock.status = MockStatus.RUNNING
    mock.pid = pid
    mock.port = port
    mock.started_at = datetime.now(timezone.utc)
    await save_mock(r, mock)
    return mock


def _start_local(mock: MockRecord, host) -> tuple[int, int]:
    """Запустить заглушку локально через subprocess + start_new_session."""
    port = _find_free_port_local(host.mock_port_min, host.mock_port_max)
    artifact_path = Path(host.working_dir) / mock.filename
    log_path = Path(host.working_dir) / f"{mock.filename}.log"
    jvm_args = mock.jvm_args.split() if mock.jvm_args else []
    port_arg = mock.port_arg_template.format(port=port)

    cmd = [host.java_path] + jvm_args + ["-jar", str(artifact_path)] + port_arg.split()

    with open(log_path, "a") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    time.sleep(_LIVENESS_CHECK_DELAY)
    if proc.poll() is not None:
        log_tail = ""
        try:
            log_tail = log_path.read_text(errors="replace")[-2000:]
        except Exception:
            pass
        raise RuntimeError(
            f"Java process (PID {proc.pid}) exited immediately (code {proc.returncode}).\n"
            f"Last log:\n{log_tail}\n\n"
            f"Hint: check 'port_arg_template'. Current: '{mock.port_arg_template}'"
        )

    logger.info("Started mock %s locally on port %d (PID %d)", mock.filename, port, proc.pid)
    return proc.pid, port


def _start_remote(mock: MockRecord, host, hostname_addr: str, ssh_port: int,
                  username: str, password: str) -> tuple[int, int]:
    """Запустить заглушку на удалённом хосте через SSH + nohup."""
    client = _build_ssh_client(hostname_addr, ssh_port, username, password)
    try:
        port = _find_free_port_ssh(client, host.mock_port_min, host.mock_port_max)
        artifact_path = f"{host.working_dir}/{mock.filename}"
        log_path = f"{host.working_dir}/{mock.filename}.log"
        jvm_args = mock.jvm_args or ""
        port_arg = mock.port_arg_template.format(port=port)

        cmd = (
            f"nohup {host.java_path} {jvm_args} "
            f"-jar {artifact_path} "
            f"{port_arg} "
            f"> {log_path} 2>&1 & echo $!"
        )
        code, out, err = _ssh_exec(client, cmd)
        if code != 0 or not out.strip().isdigit():
            raise RuntimeError(f"Failed to start mock: {err or out}")
        pid = int(out.strip())

        time.sleep(_LIVENESS_CHECK_DELAY)
        liveness_code, _, _ = _ssh_exec(client, f"kill -0 {pid} 2>/dev/null")
        if liveness_code != 0:
            _, log_tail, _ = _ssh_exec(client, f"tail -n 20 {log_path} 2>/dev/null || echo ''")
            raise RuntimeError(
                f"Java process (PID {pid}) exited immediately.\n"
                f"Last log:\n{log_tail}\n\n"
                f"Hint: check 'port_arg_template'. Current: '{mock.port_arg_template}'"
            )

        logger.info("Started mock %s on %s:%d (PID %d)", mock.filename, mock.hostname, port, pid)
        return pid, port
    finally:
        client.close()


# ---------------------------------------------------------------------------
# stop_mock
# ---------------------------------------------------------------------------

async def stop_mock(r: aioredis.Redis, filename: str) -> MockRecord:
    mock = await get_mock(r, filename)
    if mock is None:
        raise ValueError(f"Mock '{filename}' not found")
    if mock.status != MockStatus.RUNNING or mock.pid is None:
        mock.status = MockStatus.STOPPED
        mock.pid = None
        mock.port = None
        mock.started_at = None
        await save_mock(r, mock)
        return mock

    if _is_localhost(mock.hostname):
        await asyncio.to_thread(_stop_local, mock.pid, filename)
    else:
        hostname_addr, ssh_port, username, password = await _get_ssh_credentials(r, mock.hostname)
        await asyncio.to_thread(_stop_remote, mock.pid, filename,
                                hostname_addr, ssh_port, username, password)

    mock.status = MockStatus.STOPPED
    mock.pid = None
    mock.port = None
    mock.started_at = None
    await save_mock(r, mock)
    return mock


def _stop_local(pid: int, filename: str) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(5):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        os.kill(pid, signal.SIGKILL)
        logger.warning("Force-killed local mock %s (PID %d)", filename, pid)
    except ProcessLookupError:
        pass


def _stop_remote(pid: int, filename: str, hostname_addr: str, ssh_port: int,
                 username: str, password: str) -> None:
    client = _build_ssh_client(hostname_addr, ssh_port, username, password)
    try:
        _ssh_exec(client, f"kill -TERM {pid} 2>/dev/null || true")
        for _ in range(5):
            time.sleep(1)
            code, _, _ = _ssh_exec(client, f"kill -0 {pid} 2>/dev/null")
            if code != 0:
                return
        _ssh_exec(client, f"kill -KILL {pid} 2>/dev/null || true")
        logger.warning("Force-killed remote mock %s (PID %d)", filename, pid)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# delete_mock_service
# ---------------------------------------------------------------------------

async def delete_mock_service(r: aioredis.Redis, filename: str) -> None:
    mock = await get_mock(r, filename)
    if mock is None:
        return

    if mock.status == MockStatus.RUNNING:
        await stop_mock(r, filename)
        mock = await get_mock(r, filename)

    host = await get_host(r, mock.hostname)
    if host:
        if _is_localhost(mock.hostname):
            def _do_delete_local() -> None:
                (Path(host.working_dir) / filename).unlink(missing_ok=True)
                (Path(host.working_dir) / f"{filename}.log").unlink(missing_ok=True)
                logger.info("Deleted local artifact %s", filename)
            await asyncio.to_thread(_do_delete_local)
        else:
            hostname_addr, ssh_port, username, password = await _get_ssh_credentials(r, mock.hostname)
            def _do_delete_remote() -> None:
                client = _build_ssh_client(hostname_addr, ssh_port, username, password)
                try:
                    _ssh_exec(client, f"rm -f {host.working_dir}/{filename} {host.working_dir}/{filename}.log")
                    logger.info("Deleted remote artifact %s from %s", filename, mock.hostname)
                finally:
                    client.close()
            await asyncio.to_thread(_do_delete_remote)

    await delete_mock(r, filename)


# ---------------------------------------------------------------------------
# get_mock_logs
# ---------------------------------------------------------------------------

async def get_mock_logs(r: aioredis.Redis, filename: str, lines: int = 200) -> str:
    mock = await get_mock(r, filename)
    if mock is None:
        raise ValueError(f"Mock '{filename}' not found")

    host = await get_host(r, mock.hostname)
    if host is None:
        raise ValueError(f"Host '{mock.hostname}' not found")

    if _is_localhost(mock.hostname):
        def _do_logs_local() -> str:
            log_path = Path(host.working_dir) / f"{filename}.log"
            if not log_path.exists():
                return ""
            text = log_path.read_text(errors="replace")
            return "\n".join(text.splitlines()[-lines:])
        return await asyncio.to_thread(_do_logs_local)
    else:
        hostname_addr, ssh_port, username, password = await _get_ssh_credentials(r, mock.hostname)
        def _do_logs_remote() -> str:
            client = _build_ssh_client(hostname_addr, ssh_port, username, password)
            try:
                log_path = f"{host.working_dir}/{filename}.log"
                _, out, _ = _ssh_exec(client, f"tail -n {lines} {log_path} 2>/dev/null || echo ''")
                return out
            finally:
                client.close()
        return await asyncio.to_thread(_do_logs_remote)


# ---------------------------------------------------------------------------
# check_process_alive
# ---------------------------------------------------------------------------

async def check_process_alive(r: aioredis.Redis, filename: str) -> bool:
    mock = await get_mock(r, filename)
    if mock is None or mock.pid is None:
        return False

    if _is_localhost(mock.hostname):
        def _check_local() -> bool:
            try:
                os.kill(mock.pid, 0)
                return True
            except ProcessLookupError:
                return False
        return await asyncio.to_thread(_check_local)
    else:
        hostname_addr, ssh_port, username, password = await _get_ssh_credentials(r, mock.hostname)
        pid = mock.pid
        def _check_remote() -> bool:
            client = _build_ssh_client(hostname_addr, ssh_port, username, password)
            try:
                code, _, _ = _ssh_exec(client, f"kill -0 {pid} 2>/dev/null")
                return code == 0
            finally:
                client.close()
        return await asyncio.to_thread(_check_remote)
