"""REST API заглушек: /api/v1/mocks (ТЗ, раздел «REST API» → «Заглушки»)."""

from __future__ import annotations

import logging
from io import BytesIO

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from starlette.datastructures import UploadFile as StarletteUploadFile

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
from dependencies import LifecycleManagerDep, RedisClientDep
from models.mock import MockConfigUpdate, MockRateLimitUpdate, MockResponse, MockStatus
from services.lifecycle import MOCKS_REGISTRY_KEY, LifecycleManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mocks"])


def _mock_key(filename: str) -> str:
    return f"mocks:{filename}"


def _hash_to_response(filename: str, data: dict[str, str]) -> MockResponse:
    """Преобразует Hash mocks:{filename} в MockResponse."""
    status_raw = (data.get("status") or "").strip()
    try:
        st = MockStatus(status_raw)
    except ValueError:
        st = MockStatus.STOPPED

    port_s = (data.get("port") or "").strip()
    pid_s = (data.get("pid") or "").strip()
    started_s = (data.get("started_at") or "").strip()
    try:
        rate_limit = int(data.get("rate_limit") or "0")
    except ValueError:
        rate_limit = 0

    port: int | None = int(port_s) if port_s else None
    pid: int | None = int(pid_s) if pid_s else None

    return MockResponse(
        filename=filename,
        hostname=data.get("hostname") or "",
        port=port,
        pid=pid,
        status=st,
        jvm_args=data.get("jvm_args") or "",
        rate_limit=rate_limit,
        rate_limit_enabled=(data.get("rate_limit_enabled") or "false").lower() == "true",
        registered_at=data.get("registered_at") or "",
        started_at=started_s if started_s else None,
        artifact_path=data.get("artifact_path") or "",
    )


def _http_from_lifecycle_exc(exc: Exception) -> HTTPException:
    if isinstance(exc, (MockNotFoundError, HostNotFoundError, AccountNotFoundError)):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (MockAlreadyExistsError, MockAlreadyRunningError, MockNotRunningError)):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, InvalidMockFileError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
    logger.exception("Mock lifecycle error")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


async def _register_mock_background(
    lifecycle: LifecycleManager,
    file_body: bytes,
    upload_filename: str,
    hostname: str,
    jvm_args: str,
    rate_limit: int,
    auto_start: bool,
) -> None:
    """Фоновая регистрация: тело файла уже прочитано в обработчике."""
    upload = StarletteUploadFile(file=BytesIO(file_body), filename=upload_filename)
    try:
        await lifecycle.register_mock(
            upload,
            hostname,
            jvm_args,
            rate_limit,
            auto_start,
        )
    except Exception as exc:
        if isinstance(
            exc,
            (
                MockAlreadyExistsError,
                InvalidMockFileError,
                HostNotFoundError,
                AccountNotFoundError,
                LifecycleOperationError,
                SSHConnectionError,
                SCPError,
            ),
        ):
            logger.warning("Background registration %s aborted: %s", upload_filename, exc)
            return
        logger.exception("Background mock registration failed: %s", upload_filename)
        raise


@router.get("", response_model=list[MockResponse])
async def list_mocks(redis: RedisClientDep) -> list[MockResponse]:
    """Список заглушек: реестр + Pipeline HGETALL по каждому filename."""
    names = await redis.smembers(MOCKS_REGISTRY_KEY)
    if not names:
        return []
    ordered = sorted(names, key=str)
    async with redis.pipeline(transaction=False) as pipe:
        for name in ordered:
            pipe.hgetall(_mock_key(name))
        rows = await pipe.execute()
    out: list[MockResponse] = []
    for name, data in zip(ordered, rows, strict=True):
        if not data:
            continue
        out.append(_hash_to_response(str(name), data))
    return out


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def register_mock(
    background_tasks: BackgroundTasks,
    lifecycle: LifecycleManagerDep,
    redis: RedisClientDep,
    file: UploadFile = File(description="Артефакт .jar / .war"),
    hostname: str = Form(),
    jvm_args: str = Form(""),
    rate_limit: int = Form(0),
    auto_start: bool = Form(False),
) -> None:
    """Регистрация multipart/form-data; работа LifecycleManager — в BackgroundTasks, ответ 202."""
    raw_name = (file.filename or "").strip()
    if not raw_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Не указано имя файла")

    from pathlib import Path

    filename = Path(raw_name).name
    if await redis.sismember(MOCKS_REGISTRY_KEY, filename):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Заглушка уже зарегистрирована: {filename!r}",
        )

    body = await file.read()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Пустой файл артефакта")

    background_tasks.add_task(
        _register_mock_background,
        lifecycle,
        body,
        raw_name,
        hostname.strip(),
        jvm_args or "",
        rate_limit,
        auto_start,
    )


@router.get("/{filename}", response_model=MockResponse)
async def get_mock(
    filename: str,
    redis: RedisClientDep,
) -> MockResponse:
    """Детали одной заглушки по имени файла артефакта."""
    if not await redis.sismember(MOCKS_REGISTRY_KEY, filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Заглушка не найдена: {filename!r}")
    data = await redis.hgetall(_mock_key(filename))
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Заглушка не найдена: {filename!r}")
    return _hash_to_response(filename, data)


@router.delete("/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mock(
    filename: str,
    lifecycle: LifecycleManagerDep,
) -> None:
    """Удаление заглушки через LifecycleManager."""
    try:
        await lifecycle.delete_mock(filename)
    except MockNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise _http_from_lifecycle_exc(exc) from exc


@router.post("/{filename}/start", status_code=status.HTTP_204_NO_CONTENT)
async def start_mock(
    filename: str,
    lifecycle: LifecycleManagerDep,
) -> None:
    """Запуск заглушки."""
    try:
        await lifecycle.start_mock(filename)
    except MockNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise _http_from_lifecycle_exc(exc) from exc


@router.post("/{filename}/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_mock(
    filename: str,
    lifecycle: LifecycleManagerDep,
) -> None:
    """Остановка заглушки."""
    try:
        await lifecycle.stop_mock(filename)
    except MockNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise _http_from_lifecycle_exc(exc) from exc


@router.patch("/{filename}/rate-limit", response_model=MockResponse)
async def patch_mock_rate_limit(
    filename: str,
    payload: MockRateLimitUpdate,
    redis: RedisClientDep,
) -> MockResponse:
    """HSET поля rate_limit_enabled («true» / «false»)."""
    if not await redis.sismember(MOCKS_REGISTRY_KEY, filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Заглушка не найдена: {filename!r}")
    key = _mock_key(filename)
    val = "true" if payload.enabled else "false"
    await redis.hset(key, "rate_limit_enabled", val)
    data = await redis.hgetall(key)
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Заглушка не найдена: {filename!r}")
    return _hash_to_response(filename, data)


@router.patch("/{filename}/config", response_model=MockResponse)
async def patch_mock_config(
    filename: str,
    payload: MockConfigUpdate,
    redis: RedisClientDep,
) -> MockResponse:
    """HSET jvm_args и/или rate_limit при наличии в теле."""
    if not await redis.sismember(MOCKS_REGISTRY_KEY, filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Заглушка не найдена: {filename!r}")
    if payload.jvm_args is None and payload.rate_limit is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите хотя бы одно поле: jvm_args или rate_limit",
        )
    mapping: dict[str, str] = {}
    if payload.jvm_args is not None:
        mapping["jvm_args"] = payload.jvm_args
    if payload.rate_limit is not None:
        mapping["rate_limit"] = str(payload.rate_limit)
    key = _mock_key(filename)
    await redis.hset(key, mapping=mapping)
    data = await redis.hgetall(key)
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Заглушка не найдена: {filename!r}")
    return _hash_to_response(filename, data)
