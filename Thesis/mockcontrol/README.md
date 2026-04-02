# MockControl

Система централизованного управления и мониторинга имитационных сервисов (заглушек) в среде нагрузочного тестирования.

## Назначение

MockControl — программный комплекс для оркестрации Java-заглушек (.jar/.war), используемых при нагрузочном тестировании. Система позволяет загружать артефакты на удалённые хосты, управлять их жизненным циклом (запуск/остановка), проксировать трафик с поддержкой Rate Limiting и экспортировать метрики в Prometheus.

## Технологический стек

- **Python 3.10+** с asyncio
- **FastAPI** (ASGI, Uvicorn)
- **Redis** (In-Memory хранилище, AOF-персистентность)
- **asyncssh** (SSH/SCP-операции)
- **httpx** (асинхронный HTTP-клиент для проксирования)
- **cryptography** (Fernet AES-128 для шифрования паролей)
- **Jinja2** (Server-Side Rendering веб-интерфейса)

## Быстрый старт

### Предварительные требования

- Python 3.10+
- Redis 7+
- SSH-доступ к целевым хостам
- Java (OpenJDK 17+) на целевых хостах

### Установка

```bash
# Клонирование
git clone <repository-url>
cd mockcontrol

# Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# Зависимости
pip install -r requirements.txt

# Конфигурация
cp .env.example .env
# Отредактируйте .env при необходимости
```

### Запуск

```bash
# Запуск Redis (если не запущен)
redis-server --appendonly yes &

# Запуск MockControl
uvicorn mockcontrol.main:app --host 0.0.0.0 --port 8000
```

Интерфейс: http://localhost:8000/dashboard
API-документация: http://localhost:8000/api/docs

### Запуск тестов

```bash
pip install -r requirements-dev.txt
pytest
```

## Структура проекта

```
mockcontrol/
├── main.py              # Точка входа, FastAPI app, lifespan
├── config.py            # Pydantic Settings (.env)
├── dependencies.py      # DI-контейнер
│
├── core/                # Ядро бизнес-логики
│   ├── lifecycle.py     # Управление жизненным циклом заглушек
│   ├── proxy.py         # Прозрачный обратный прокси (httpx)
│   ├── rate_limiter.py  # Fixed Window Counter (Lua-скрипт)
│   ├── crypto.py        # Шифрование паролей (Fernet)
│   ├── metrics.py       # Экспорт метрик Prometheus
│   └── host_checker.py  # Фоновая проверка хостов
│
├── api/                 # REST API
│   ├── v1/
│   │   ├── mocks.py     # CRUD заглушек + start/stop
│   │   ├── hosts.py     # CRUD хостов + проверка
│   │   ├── accounts.py  # CRUD учётных записей SSH
│   │   ├── settings.py  # Глобальные настройки
│   │   ├── logs.py      # Журналы процессов
│   │   └── proxy.py     # Catch-all прокси-роут
│   └── metrics.py       # GET /metrics
│
├── models/              # Pydantic-модели
│   ├── mock.py          # MockConfig, MockResponse, ...
│   ├── host.py          # HostConfig, HostResponse, ...
│   ├── account.py       # AccountConfig, AccountResponse, ...
│   ├── settings.py      # GlobalSettings
│   └── common.py        # MessageResponse, DashboardSummary
│
├── services/            # Сервисный слой (Redis)
│   ├── mock_service.py
│   ├── host_service.py
│   ├── account_service.py
│   └── settings_service.py
│
├── ssh/                 # SSH/SCP-операции
│   ├── client.py        # AsyncSSHClient
│   └── operations.py    # copy_artifact, start/stop, find_port
│
├── utils/               # Утилиты
│   ├── __init__.py      # validate_artifact_filename
│   └── port_finder.py   # Работа с TCP-портами
│
├── web/                 # Веб-интерфейс (Jinja2 SSR)
│   ├── routes.py        # HTML-роуты
│   ├── templates/       # Jinja2-шаблоны
│   └── static/          # CSS, JS
│
└── tests/               # Pytest-тесты
    ├── conftest.py
    ├── test_rate_limiter.py
    ├── test_services.py
    ├── test_lifecycle.py
    ├── test_proxy.py
    └── test_crypto_and_utils.py
```

## API-эндпоинты

| Метод | Путь | Назначение |
|-------|------|------------|
| GET | `/dashboard` | Веб-интерфейс |
| GET | `/metrics` | Метрики Prometheus |
| GET/POST | `/api/v1/mocks` | Список / регистрация заглушек |
| POST | `/api/v1/mocks/{name}/start` | Запуск заглушки |
| POST | `/api/v1/mocks/{name}/stop` | Остановка заглушки |
| PATCH | `/api/v1/mocks/{name}/rate-limit` | Переключение Rate Limiter |
| GET/POST | `/api/v1/hosts` | Список / регистрация хостов |
| GET/POST | `/api/v1/accounts` | Список / создание учётных записей |
| GET/PUT | `/api/v1/settings` | Глобальные настройки |
| GET | `/api/v1/logs/{name}` | Журналы процессов |
| ANY | `/{mock_name}/{path}` | Прокси к заглушке |

## Целевая платформа

ОС Astra Linux Special Edition 1.7+, совместим с любыми Linux-дистрибутивами.

## Лицензия

Разработано в рамках ВКР в Московском Политехническом Университете, 2026.
