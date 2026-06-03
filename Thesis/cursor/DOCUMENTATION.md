# MockControl — Документация

MockControl — это платформа для управления Java-мок-серверами (stub-сервисами) на удалённых и локальных хостах. Через единый веб-интерфейс и REST API можно загружать JAR-файлы, запускать/останавливать Java-процессы, проксировать к ним HTTP-запросы, собирать логи и метрики.

---

## Структура проекта

```
cursor/
├── main.py               # Точка входа, lifespan, регистрация роутеров
├── config.py             # Конфигурация через переменные окружения
├── dependencies.py       # Инициализация и DI всех сервисов
├── api/
│   ├── proxy_router.py   # Catch-all прокси-роутер
│   └── v1/
│       ├── mocks.py      # CRUD + управление жизненным циклом моков
│       ├── hosts.py      # CRUD хостов
│       ├── accounts.py   # CRUD SSH-аккаунтов
│       ├── settings.py   # Глобальные настройки
│       ├── logs.py       # Получение логов моков
│       └── metrics.py    # Prometheus /metrics
├── core/
│   ├── crypto.py         # Шифрование паролей (Fernet)
│   ├── ssh_pool.py       # Пул постоянных SSH-соединений
│   ├── redis_client.py   # Инициализация Redis + документация ключей
│   ├── host_resolver.py  # Определение локальности хоста
│   └── exceptions.py     # Иерархия доменных исключений
├── services/
│   ├── lifecycle.py      # Загрузка, запуск, остановка, удаление моков
│   ├── proxy.py          # HTTP-прокси через httpx с трассировкой
│   ├── health_checker.py # Фоновая проверка хостов и моков
│   ├── log_collector.py  # Фоновый сбор логов моков
│   ├── metrics.py        # Запись и экспорт метрик Prometheus
│   ├── rate_limiter.py   # Fixed Window ограничитель запросов
│   └── settings_service.py # CRUD для хостов, аккаунтов, настроек
├── models/
│   ├── mock.py           # Схемы мока (Create, Config, Response, Update)
│   ├── host.py           # Схемы хоста
│   ├── account.py        # Схемы SSH-аккаунта
│   └── settings.py       # Схема глобальных настроек
└── web/
    ├── routes.py         # SSR-страницы (Jinja2)
    ├── templates/        # HTML-шаблоны (dashboard, logs, settings)
    └── static/           # CSS, JS, шрифты
```

---

## Хранилище данных: Redis

Всё состояние системы хранится в Redis. Реляционной БД нет.

| Ключ Redis | Тип | Содержимое |
|---|---|---|
| `mocks:{filename}` | Hash | hostname, port, PID, status, JVM args, rate limit |
| `mocks:registry` | Set | список имён JAR-файлов |
| `hosts:{hostname}` | Hash | SSH-порт, UUID аккаунта, рабочая директория, диапазон портов |
| `hosts:registry` | Set | список имён хостов |
| `accounts:{uuid}` | Hash | username, зашифрованный пароль, описание |
| `accounts:registry` | Set | список UUID аккаунтов |
| `settings:global` | Hash | все глобальные настройки |
| `logs:{filename}` | List | строки вывода мока (RPUSH + LTRIM) |
| `rate:{filename}:{window}` | String | счётчик запросов в текущем окне |
| `metrics:proxy_total:{filename}` | String | всего запросов через прокси |
| `metrics:proxy_stage_sum_ms:{filename}` | Hash | сумма латентностей по стадиям |
| `metrics:proxy_outcome_total:{filename}` | Hash | исходы запросов (ok, timeout, error…) |

---

## Модули

### `main.py` — Точка входа

Запускает FastAPI-приложение через Uvicorn. Регистрирует роутеры в строгом порядке: SSR-страницы → API v1 → /metrics → /static → catch-all прокси (последним, иначе перехватит всё).

**Singleton-блокировка фоновых задач.** При запуске нескольких воркеров (несколько процессов Uvicorn) фоновые задачи должны работать только в одном воркере. Реализовано через Redis-lock: воркер делает `SET key NX EX`, получая эксклюзивный доступ, и периодически продлевает его Lua-скриптом. Если воркер умирает — lock истекает и его подхватывает другой.

```python
# Атомарное продление lock через Lua (compare-and-delete + set)
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
return nil
"""
```

**Количество воркеров** вычисляется как `CPU_COUNT × 2`, минимум 1. Можно переопределить переменной `UVICORN_WORKERS`.

---

### `config.py` — Конфигурация

Pydantic `BaseSettings` — читает переменные окружения или `.env` файл.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Подключение к Redis |
| `FERNET_KEY_PATH` | `/etc/mockcontrol/fernet.key` | Путь к ключу шифрования |
| `TEMP_UPLOAD_DIR` | `/tmp/mockcontrol` | Временная директория для загрузок |
| `DEFAULT_RATE_LIMIT_WINDOW` | `1` сек | Размер окна rate limiter |
| `DEFAULT_HOST_CHECK_INTERVAL` | `30` сек | Интервал health check |
| `DEFAULT_PROXY_TIMEOUT` | `10` сек | Таймаут HTTP-прокси |
| `DEFAULT_LOG_RETENTION_LINES` | `1000` строк | Размер буфера логов в Redis |
| `UVICORN_WORKERS` | `None` (авто) | Число воркеров |

---

### `core/crypto.py` — Шифрование

Шифрует SSH-пароли аккаунтов алгоритмом **Fernet** (AES-128-CBC + HMAC-SHA256) из библиотеки `cryptography`.

При первом запуске генерирует ключ и сохраняет его в файл с правами `600` (только владелец). При последующих запусках читает ключ из файла.

```python
class CryptoService:
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...
```

Пароли хранятся в Redis только в зашифрованном виде и расшифровываются непосредственно перед SSH-подключением.

---

### `core/ssh_pool.py` — Пул SSH-соединений

Поддерживает постоянные SSH-соединения к хостам через `asyncssh`. Не открывает новое соединение на каждую операцию.

**Структура хранения:** для каждого хоста хранится `_HostEntry` — asyncio-lock + соединение + последние использованные credentials. Lock гарантирует, что одновременно только одна корутина выполняет connect/probe для конкретного хоста.

**Логика `get_connection()`:**
1. Если соединения нет — создать.
2. Если соединение есть — прозондировать командой `true`.
3. Если probe упал — переподключиться.

**Методы:**

```python
async def run_command(hostname, username, password, command, ...) -> str
async def scp_upload(hostname, username, password, local_path, remote_path, ...) -> None
```

Загрузка файлов идёт через SFTP (не через scp), что надёжнее и не требует отдельного клиента.

---

### `core/host_resolver.py` — Определение локальности хоста

Определяет, является ли hostname локальной машиной. Результат кешируется через `@lru_cache`.

Проверяется:
- Литеральные значения: `localhost`, `127.0.0.1`, `::1`
- `socket.gethostname()`
- Все IP из `getaddrinfo()` и `gethostbyname_ex()`
- Локальный IP через подключение к внешнему адресу (определяет реальный интерфейс)

Это важно для `LifecycleManager`: на локальном хосте файлы копируются через `shutil`, процессы запускаются через `asyncio.create_subprocess_exec`, а не через SSH.

---

### `services/lifecycle.py` — Жизненный цикл мока

Центральный сервис управления моками. Отвечает за всё от загрузки JAR до убийства процесса.

**Регистрация (`register_mock`):**
1. Принять файл во временную директорию.
2. Скопировать на целевой хост (локально — `shutil.copyfile`, удалённо — SFTP).
3. Записать конфигурацию в Redis.
4. Если `auto_start=True` — запустить.

**Запуск (`start_mock`):**
1. Выбрать свободный порт: локально — через `socket.bind()`, удалённо — через `ss -tlnp` или `netstat`.
2. Сформировать команду: `java {jvm_args} -jar {artifact} --server.port={port}`.
3. Локально — `asyncio.create_subprocess_exec(..., start_new_session=True)`. `start_new_session=True` нужен, чтобы процесс не умер вместе с воркером.
4. Удалённо — SSH-команда с перенаправлением stdout/stderr в лог-файл на хосте.
5. Записать PID и порт в Redis, статус → `RUNNING`.

**Остановка (`stop_mock`):**
- Локально — `os.killpg(os.getpgid(pid), signal.SIGTERM)`.
- Удалённо — `kill -TERM {pid}` по SSH.

---

### `services/proxy.py` — HTTP-прокси

Проксирует входящие HTTP-запросы к работающим мок-серверам через `httpx.AsyncClient`.

**`ProxyClientRegistry`** — реестр клиентов: один `httpx.AsyncClient` на мок, с persistent keep-alive (до 1000 соединений, 60 сек TTL). Клиент создаётся при первом обращении, удаляется при остановке мока.

**`proxy_request()`** использует `httpx` trace hooks для замера времени по стадиям:

| Стадия | Что измеряется |
|---|---|
| `httpx_pool_wait_ms` | Ожидание соединения из пула |
| `httpx_client_request_ms` | Отправка запроса |
| `httpx_response_read_ms` | Чтение тела ответа |
| `httpx_total_ms` | Полное время |

При таймауте возвращает `504`, при ошибке соединения — `502`. Статус и заголовки upstream прозрачно передаются клиенту.

---

### `services/rate_limiter.py` — Rate Limiter

Реализует алгоритм **Fixed Window Counter** на базе Redis.

**Ключ:** `rate:{filename}:{window}`, где `window = floor(unix_time / window_size)`.

**Логика:**
1. `INCR key` (атомарно).
2. Если счётчик стал `1` (первый запрос в окне) — установить `EXPIRE = window_size × 2`.
3. Если счётчик ≤ лимита → пропустить, иначе → отклонить.

Умножение на 2 в TTL даёт небольшой запас для граничных случаев между окнами.

---

### `services/health_checker.py` — Проверка здоровья

Фоновая задача (singleton, один воркер). Работает в бесконечном цикле с интервалом из глобальных настроек.

**Проверка хостов:**
- Локальный хост всегда `AVAILABLE`.
- Удалённый — SSH-подключение с командой `true`. Успех → `AVAILABLE`, ошибка → `UNAVAILABLE`.

**Проверка моков (статус `RUNNING`):**
- Проверить, жив ли PID: локально — `os.kill(pid, 0)`, удалённо — `kill -0 {pid}` по SSH.
- Сделать HTTP GET `/actuator` на порт мока.
- Если HTTP-ответ нормальный — мок здоров.
- Если 3 подряд провала — статус → `ERROR`.

Credentials для SSH читаются из Redis на каждой итерации, что позволяет обновить пароль без перезапуска.

---

### `services/log_collector.py` — Сбор логов

Фоновая задача (singleton). Опрашивает лог-файлы всех `RUNNING` моков каждые 2 секунды.

**Хранит состояние в памяти:**
- `_byte_positions: dict[filename, int]` — позиция в файле (не читать уже прочитанное).
- `_line_pending: dict[filename, str]` — неполная строка на конце буфера (ждёт `\n`).

**Локальный мок:** читает файл через `aiofiles` с позиции `_byte_positions[filename]`.

**Удалённый мок:** выполняет `tail -c +{N} {log_path}` по SSH, где `N` — следующий байт после последней прочитанной позиции.

Строки пушатся в Redis List `logs:{filename}` через `RPUSH`, после чего `LTRIM` обрезает список до `log_retention_lines`.

---

### `services/metrics.py` — Метрики

**Запись (`record_proxy_observation`):** атомарный Redis pipeline на каждый проксированный запрос. Инкрементирует счётчики и накапливает суммы латентностей.

**Экспорт (`collect_metrics`):** читает все метрики из Redis и формирует текст в формате Prometheus text exposition.

**Метрики:**

| Метрика | Тип | Описание |
|---|---|---|
| `mocks_total` | Gauge | Всего моков |
| `mocks_running` | Gauge | Моков в статусе RUNNING |
| `mocks_errors` | Gauge | Моков в статусе ERROR |
| `hosts_available` | Gauge | Доступных хостов |
| `proxy_requests_total` | Counter | Всего запросов через прокси |
| `rate_limit_rejected_total` | Counter | Отклонено rate limiter |
| `proxy_stage_latency_*` | Gauge | Средняя латентность по стадии |
| `proxy_outcome_total` | Counter | Исходы запросов по типам |
| `proxy_upstream_status_total` | Counter | HTTP-статусы ответов upstream |

---

### `api/proxy_router.py` — Прокси-роутер

Catch-all роутер — перехватывает любой запрос вида `/{mock_name}/...`.

**Горячий путь (один Redis pipeline):**
1. Получить конфиг мока + глобальные настройки.
2. Проверить статус (`RUNNING`), иначе `503`.
3. Проверить rate limit, если включён, иначе `429`.
4. Получить или создать `httpx.AsyncClient`.
5. Проксировать запрос с замером времени.
6. Записать метрики (fire-and-forget, не блокирует ответ).

---

### `services/settings_service.py` — CRUD сервис

**Хосты:** при удалении проверяет, что к хосту не привязан ни один мок (иначе `HostHasMocksError`). При создании проверяет существование указанного аккаунта.

**Аккаунты:** при создании генерирует UUID, шифрует пароль через `CryptoService`. При обновлении — перешифровывает пароль, если он изменился. При удалении проверяет, что аккаунт не используется ни одним хостом (иначе `AccountInUseError`).

**Настройки:** если `settings:global` не существует в Redis — инициализирует значениями из `config.py`.

---

### `core/exceptions.py` — Исключения

Доменные исключения для чёткого разграничения ошибок в бизнес-логике. API-роутеры ловят их и возвращают соответствующие HTTP-коды.

```
MockNotFoundError / MockAlreadyExistsError
HostNotFoundError / HostAlreadyExistsError / HostHasMocksError
AccountNotFoundError / AccountInUseError
MockNotRunningError / MockAlreadyRunningError
SSHConnectionError / SCPError
InvalidMockFileError / LifecycleOperationError
```

---

## Ключевые потоки данных

### Регистрация и запуск мока

```
POST /api/v1/mocks (multipart: JAR + параметры)
  → LifecycleManager.register_mock()
    → Сохранить JAR во temp_upload_dir
    → Скопировать на хост (shutil / SFTP)
    → Записать конфиг в Redis Hash mocks:{filename}
    → Добавить в Set mocks:registry
    → Если auto_start: start_mock()
      → Найти свободный порт
      → Запустить java -jar
      → Записать PID + port в Redis
```

### Обработка входящего HTTP-запроса

```
GET /{mock_name}/api/users
  → proxy_router
    → Redis pipeline: mocks:{mock_name} + settings:global
    → Проверить status == RUNNING
    → RateLimiter.check()
    → ProxyClientRegistry.get_or_create()
    → proxy_request() → httpx → upstream mock
    → Вернуть ответ клиенту
    → asyncio.create_task(record_proxy_observation())  # не блокирует
```

### Фоновые задачи

```
Один воркер держит Redis lock
  ├── HealthChecker (каждые N сек)
  │     ├── Для каждого хоста: SSH probe
  │     └── Для каждого RUNNING мока: PID check + HTTP /actuator
  └── LogCollector (каждые 2 сек)
        └── Для каждого RUNNING мока: читать новые байты лог-файла → Redis List
```

---

## Зависимости

| Библиотека | Для чего |
|---|---|
| `fastapi` | Web-фреймворк |
| `uvicorn` | ASGI-сервер |
| `redis` | Клиент Redis (async) |
| `asyncssh` | SSH/SFTP клиент |
| `httpx` | HTTP-прокси клиент |
| `cryptography` | Fernet-шифрование |
| `pydantic` / `pydantic-settings` | Валидация данных и конфиг |
| `aiofiles` | Асинхронное чтение файлов |
| `jinja2` | SSR HTML-шаблоны |
| `python-multipart` | Парсинг multipart/form-data |
