"""
Высокоуровневые операции на целевых хостах через SSH/SCP.

Предоставляет бизнес-ориентированный интерфейс для Lifecycle Manager:
копирование артефактов, запуск/остановка Java-процессов, поиск портов.
"""

import logging
from typing import Optional

from mockcontrol.ssh.client import AsyncSSHClient, CommandResult

logger = logging.getLogger(__name__)


async def copy_artifact(
    client: AsyncSSHClient,
    local_path: str,
    remote_dir: str,
    filename: str,
) -> bool:
    """
    Скопировать артефакт на целевой хост и выставить права.

    Args:
        client: SSH-клиент с установленными credentials.
        local_path: Путь к файлу на сервере управляющего приложения.
        remote_dir: Рабочая директория на целевом хосте.
        filename: Имя файла артефакта.

    Returns:
        True при успехе.
    """
    # Убедиться, что рабочая директория существует
    mkdir_result = await client.run_command(f"mkdir -p {remote_dir}")
    if not mkdir_result.success:
        logger.error("Failed to create remote dir %s: %s", remote_dir, mkdir_result.stderr)
        return False

    remote_path = f"{remote_dir}/{filename}"
    if not await client.copy_to(local_path, remote_path):
        return False

    # Установить права на исполнение
    chmod_result = await client.run_command(f"chmod 644 {remote_path}")
    if not chmod_result.success:
        logger.warning("chmod failed for %s: %s", remote_path, chmod_result.stderr)

    return True


async def find_free_port(
    client: AsyncSSHClient,
    port_min: int,
    port_max: int,
) -> Optional[int]:
    """
    Найти свободный TCP-порт в заданном диапазоне на удалённом хосте.

    Проверяет занятость портов через ss (socket statistics).

    Returns:
        Номер свободного порта или None, если все заняты.
    """
    # Получить список занятых портов в диапазоне одной командой
    cmd = (
        f"ss -tlnH | awk '{{print $4}}' | "
        f"grep -oP ':\\K[0-9]+$' | "
        f"sort -un"
    )
    result = await client.run_command(cmd)
    occupied: set[int] = set()
    if result.success and result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            try:
                occupied.add(int(line.strip()))
            except ValueError:
                continue

    for port in range(port_min, port_max + 1):
        if port not in occupied:
            return port

    logger.error("No free ports in range %d-%d", port_min, port_max)
    return None


async def start_java_process(
    client: AsyncSSHClient,
    java_path: str,
    artifact_path: str,
    port: int,
    jvm_args: str = "",
    log_file: Optional[str] = None,
) -> Optional[int]:
    """
    Запустить Java-процесс в фоновом режиме через nohup.

    Args:
        java_path: Абсолютный путь к исполняемому файлу Java.
        artifact_path: Путь к .jar/.war файлу на целевом хосте.
        port: TCP-порт для заглушки.
        jvm_args: Дополнительные аргументы JVM.
        log_file: Путь к файлу лога на хосте (stdout+stderr).

    Returns:
        PID запущенного процесса или None при ошибке.
    """
    if log_file is None:
        log_file = f"{artifact_path}.log"

    # Формируем команду запуска
    args_str = jvm_args.strip()
    cmd = (
        f"nohup {java_path} {args_str} "
        f"-jar {artifact_path} "
        f"--server.port={port} "
        f"> {log_file} 2>&1 & echo $!"
    )

    result = await client.run_command(cmd, timeout=15.0)
    if not result.success:
        logger.error("Failed to start Java process: %s", result.stderr)
        return None

    pid_str = result.stdout.strip().splitlines()[-1]
    try:
        pid = int(pid_str)
        logger.info("Started Java process PID=%d on port %d", pid, port)
        return pid
    except ValueError:
        logger.error("Could not parse PID from output: %s", result.stdout)
        return None


async def stop_process(client: AsyncSSHClient, pid: int) -> bool:
    """
    Остановить процесс: сначала SIGTERM, затем SIGKILL через 5 сек.

    Returns:
        True, если процесс успешно завершён.
    """
    # Проверить, существует ли процесс
    check = await client.run_command(f"kill -0 {pid} 2>/dev/null && echo alive || echo dead")
    if "dead" in check.stdout:
        logger.info("Process PID=%d already dead", pid)
        return True

    # Корректное завершение — SIGTERM
    await client.run_command(f"kill {pid}")

    # Ожидание завершения (до 5 секунд)
    wait_cmd = (
        f"for i in $(seq 1 10); do "
        f"  kill -0 {pid} 2>/dev/null || {{ echo stopped; exit 0; }}; "
        f"  sleep 0.5; "
        f"done; echo timeout"
    )
    wait_result = await client.run_command(wait_cmd, timeout=10.0)

    if "stopped" in wait_result.stdout:
        logger.info("Process PID=%d stopped gracefully", pid)
        return True

    # Принудительное завершение — SIGKILL
    logger.warning("Process PID=%d did not stop gracefully, sending SIGKILL", pid)
    await client.run_command(f"kill -9 {pid}")
    return True


async def check_process_alive(client: AsyncSSHClient, pid: int) -> bool:
    """Проверить, жив ли процесс на удалённом хосте."""
    result = await client.run_command(f"kill -0 {pid} 2>/dev/null && echo alive || echo dead")
    return "alive" in result.stdout


async def delete_remote_file(client: AsyncSSHClient, remote_path: str) -> bool:
    """Удалить файл на удалённом хосте."""
    result = await client.run_command(f"rm -f {remote_path}")
    return result.success


async def read_log_tail(
    client: AsyncSSHClient,
    log_file: str,
    lines: int = 200,
) -> str:
    """Прочитать последние N строк лога с удалённого хоста."""
    result = await client.run_command(f"tail -n {lines} {log_file} 2>/dev/null")
    if result.success:
        return result.stdout
    return f"[Log unavailable: {result.stderr}]"
