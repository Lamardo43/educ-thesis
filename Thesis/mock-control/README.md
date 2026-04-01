# MockControl

Автоматизированная информационная система управления распределённой инфраструктурой имитационных сервисов (заглушек) для нагрузочного тестирования.

**Стек:** Python 3.10+ · FastAPI · Redis · Paramiko · HTTPX · Jinja2 · Bootstrap 5

---

## Быстрый старт

### Вариант А — напрямую (redis-server на машине)

```bash
# 1. Установить Redis (если не установлен)
sudo apt install redis-server   # Astra Linux / Debian / Ubuntu

# 2. Установить зависимости Python
pip install -r requirements.txt

# 3. Скопировать конфиг окружения
cp .env.example .env

# 4. Запустить приложение
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

При запуске приложение **автоматически поднимает redis-server** если он ещё не запущен на указанном порту.
Данные сохраняются в `./data/redis/` (AOF), Fernet-ключ генерируется в `./data/fernet.key` автоматически.

Веб-интерфейс: http://localhost:8000

---

### Вариант Б — Docker Compose

```bash
docker compose up -d
```

Redis и приложение стартуют вместе, данные персистируются в named volumes.

```bash
docker compose logs -f     # логи в реальном времени
docker compose down        # остановить
```

---

## Структура проекта

```
mock-control/
├── app/
│   ├── main.py                  # Точка входа FastAPI + lifespan
│   ├── config.py                # Настройки (pydantic-settings, .env)
│   ├── core/
│   │   ├── redis_client.py      # Подключение к Redis (aioredis)
│   │   ├── redis_server.py      # EmbeddedRedisManager: авто-запуск redis-server
│   │   └── crypto.py            # AES-128 Fernet шифрование паролей SSH
│   ├── models/                  # Pydantic-схемы (MockRecord, HostRecord, ...)
│   ├── repositories/            # CRUD-операции в Redis
│   ├── services/
│   │   ├── lifecycle.py         # Управление процессами (SSH/SCP/nohup)
│   │   ├── proxy.py             # Прозрачный обратный прокси (httpx)
│   │   ├── rate_limiter.py      # Fixed Window Counter (Redis INCR/EXPIRE)
│   │   ├── metrics.py           # Prometheus exposition format
│   │   ├── host_checker.py      # Фоновая проверка SSH-доступности хостов
│   │   └── startup_reconciler.py# Reconcile RUNNING-заглушек при старте
│   ├── api/                     # REST API роутеры (/api/v1/...)
│   ├── web/                     # SSR Jinja2 страницы
│   └── proxy_handler.py         # Catch-all proxy /{mock}/{path}
├── templates/                   # HTML-шаблоны (Bootstrap 5, тёмная тема)
├── static/                      # CSS + JS
├── tests/                       # pytest: unit + integration
├── Dockerfile
└── docker-compose.yml
```

---

## REST API

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/v1/mocks` | Список всех заглушек |
| POST | `/api/v1/mocks` | Регистрация заглушки (multipart) |
| GET | `/api/v1/mocks/{name}` | Информация о заглушке |
| POST | `/api/v1/mocks/{name}/start` | Запуск процесса |
| POST | `/api/v1/mocks/{name}/stop` | Остановка процесса |
| DELETE | `/api/v1/mocks/{name}` | Удаление (файл + запись Redis) |
| PATCH | `/api/v1/mocks/{name}/rate-limit` | Переключение Rate Limiter |
| GET | `/api/v1/mocks/{name}/logs` | Журнал процесса (SSH tail) |
| GET/POST/PUT/... | `/{name}/{path}` | Проксирование к заглушке |
| GET | `/api/v1/hosts` | Список хостов |
| POST | `/api/v1/hosts` | Регистрация хоста |
| PUT | `/api/v1/hosts/{hostname}` | Обновление хоста |
| DELETE | `/api/v1/hosts/{hostname}` | Удаление хоста |
| GET | `/api/v1/accounts` | Учётные записи SSH |
| POST | `/api/v1/accounts` | Создание учётной записи |
| PUT | `/api/v1/accounts/{uuid}` | Обновление учётной записи |
| DELETE | `/api/v1/accounts/{uuid}` | Удаление учётной записи |
| GET | `/api/v1/settings` | Глобальные настройки |
| PUT | `/api/v1/settings` | Сохранение настроек |
| GET | `/metrics` | Prometheus метрики |

---

## Тесты

```bash
pytest tests/ -v
```

---

## Развёртывание как systemd-сервис (Astra Linux)

```ini
# /etc/systemd/system/mock-control.service
[Unit]
Description=MockControl
After=network.target

[Service]
User=mock-control
WorkingDirectory=/opt/mock-control
EnvironmentFile=/opt/mock-control/.env
ExecStart=/opt/mock-control/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mock-control
```

Redis запускается автоматически приложением — отдельная systemd-служба для Redis не нужна.
