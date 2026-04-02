"""
Точка входа приложения MockControl.

Создаёт FastAPI-приложение, подключает все роутеры
и управляет жизненным циклом зависимостей через lifespan.

Порядок подключения роутеров важен:
1. /static/*         — статические файлы (CSS, JS)
2. /metrics          — служебный эндпоинт Prometheus
3. /dashboard, etc.  — HTML-страницы (Jinja2 SSR)
4. /api/v1/*         — REST API управления
5. /{mock_name}/**   — catch-all прокси (ПОСЛЕДНИМ, чтобы не перехватывать)
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mockcontrol.api import metrics_router, proxy_router, v1_router
from mockcontrol.dependencies import init_container, shutdown_container
from mockcontrol.web import web_router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Путь к каталогу статических файлов
STATIC_DIR = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Жизненный цикл приложения.

    startup:
        - Подключение к Redis
        - Инициализация всех компонентов ядра (DI-контейнер)
        - Reconciliation состояний заглушек
        - Запуск фоновых задач (HostChecker)

    shutdown:
        - Остановка фоновых задач
        - Закрытие пула соединений httpx (ProxyEngine)
        - Закрытие соединения с Redis
    """
    logger.info("MockControl starting up...")
    await init_container()
    logger.info("MockControl ready")

    yield

    logger.info("MockControl shutting down...")
    await shutdown_container()
    logger.info("MockControl stopped")


def create_app() -> FastAPI:
    """Фабрика приложения FastAPI."""

    app = FastAPI(
        title="MockControl",
        description=(
            "Система централизованного управления и мониторинга "
            "имитационных сервисов (заглушек) в среде нагрузочного тестирования"
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # --- Статические файлы (CSS, JS) ---
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- Роутеры (порядок важен!) ---

    # 1. Prometheus metrics
    app.include_router(metrics_router)

    # 2. HTML-страницы веб-интерфейса (/, /dashboard, /settings, /logs)
    app.include_router(web_router)

    # 3. REST API v1
    app.include_router(v1_router)

    # 4. Catch-all прокси — ПОСЛЕДНИМ
    app.include_router(proxy_router)

    return app


# Экземпляр приложения для Uvicorn: uvicorn mockcontrol.main:app
app = create_app()
