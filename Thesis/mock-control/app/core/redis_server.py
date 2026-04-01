"""
EmbeddedRedisManager — запускает redis-server как дочерний процесс,
если к Redis ещё нельзя подключиться (т.е. внешний экземпляр не найден).

Жизненный цикл управляется из lifespan() в main.py:
  await redis_manager.start()   # на входе
  await redis_manager.stop()    # на выходе

Конфигурация Redis:
  - AOF-персистентность (appendonly yes, appendfsync everysec)
  - данные хранятся в REDIS_DATA_DIR (по умолчанию ./data/redis)
  - порт берётся из REDIS_URL

Если redis-server не установлен — выбрасывается понятное исключение
с инструкцией по установке.
"""

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class EmbeddedRedisManager:
    def __init__(self, host: str, port: int, data_dir: str) -> None:
        self.host = host
        self.port = port
        self.data_dir = Path(data_dir)
        self._process: subprocess.Popen | None = None
        self._we_started_it = False  # True только если мы сами подняли процесс

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Запустить Redis если он ещё не доступен.
        Если уже доступен (внешний) — ничего не делаем.
        """
        if self._is_redis_available():
            logger.info("Redis already available at %s:%d — skipping embedded start", self.host, self.port)
            return

        self._ensure_redis_installed()
        self._ensure_data_dir()
        await self._launch()

    async def stop(self) -> None:
        """Остановить Redis, только если мы его сами запустили."""
        if not self._we_started_it or self._process is None:
            return

        logger.info("Stopping embedded Redis (PID %d)…", self._process.pid)
        self._process.terminate()
        try:
            await asyncio.to_thread(self._process.wait, timeout=10)
            logger.info("Embedded Redis stopped gracefully")
        except subprocess.TimeoutExpired:
            self._process.kill()
            logger.warning("Embedded Redis force-killed after timeout")
        finally:
            self._process = None
            self._we_started_it = False

    # ── Private helpers ───────────────────────────────────────────────────

    def _is_redis_available(self) -> bool:
        """Проверить TCP-доступность Redis без внешних зависимостей."""
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _ensure_redis_installed() -> None:
        if shutil.which("redis-server") is None:
            raise RuntimeError(
                "redis-server not found in PATH.\n"
                "Install it with:\n"
                "  Astra Linux / Debian/Ubuntu:  sudo apt install redis-server\n"
                "  RHEL/CentOS:                  sudo dnf install redis\n"
                "  macOS (Homebrew):             brew install redis\n"
                "  Windows:                      use WSL or https://github.com/tporadowski/redis"
            )

    def _ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _build_config(self) -> list[str]:
        """
        Передаём конфиг прямо через аргументы командной строки —
        не нужен отдельный redis.conf файл.
        """
        return [
            "redis-server",
            "--port",            str(self.port),
            "--bind",            self.host,
            "--dir",             str(self.data_dir.resolve()),
            "--appendonly",      "yes",
            "--appendfsync",     "everysec",
            "--appendfilename",  "appendonly.aof",
            "--loglevel",        "notice",
            "--save",            "900 1",   # RDB snapshot как дополнительный бэкап
            "--save",            "300 10",
            "--save",            "60 10000",
            "--protected-mode",  "no",       # слушаем только 127.0.0.1 — безопасно
        ]

    async def _launch(self) -> None:
        cmd = self._build_config()
        logger.info("Starting embedded Redis: %s", " ".join(cmd))

        self._process = await asyncio.to_thread(
            subprocess.Popen,
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._we_started_it = True

        # Ждём готовности Redis (до 10 секунд)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                # Процесс уже завершился — что-то пошло не так
                out = self._process.stdout.read() if self._process.stdout else ""
                raise RuntimeError(
                    f"redis-server exited unexpectedly (code {self._process.returncode}).\n{out}"
                )
            if self._is_redis_available():
                logger.info(
                    "Embedded Redis is ready on %s:%d (PID %d)",
                    self.host, self.port, self._process.pid,
                )
                # Запускаем фоновое чтение stdout чтобы буфер не переполнился
                asyncio.create_task(self._drain_output())
                return
            await asyncio.sleep(0.2)

        # Timeout — убиваем и сообщаем
        self._process.kill()
        raise RuntimeError(
            f"redis-server did not become available on port {self.port} within 10 seconds."
        )

    async def _drain_output(self) -> None:
        """Читаем stdout Redis в фоне, логируем строки уровня WARNING и выше."""
        if self._process is None or self._process.stdout is None:
            return
        try:
            for line in self._process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # Redis-формат: pid:role timestamp loglevel message
                # loglevel: . (debug) - (verbose) * (notice) # (warning)
                if " # " in line:
                    logger.warning("[redis] %s", line)
                elif " * " in line:
                    logger.debug("[redis] %s", line)
        except ValueError:
            pass  # pipe closed on shutdown
