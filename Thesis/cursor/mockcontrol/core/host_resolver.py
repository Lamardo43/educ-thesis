"""Определение, является ли целевой хост локальной машиной (без SSH)."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_local_hosts: Optional[FrozenSet[str]] = None


def _normalize_token(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.lower()


def _add_ip_string(bucket: set[str], raw: str) -> None:
    if not raw:
        return
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    try:
        bucket.add(str(ipaddress.ip_address(raw)))
    except ValueError:
        bucket.add(raw.lower())


def _gather_interface_ips_netifaces() -> set[str]:
    import netifaces

    out: set[str] = set()
    for iface in netifaces.interfaces():
        try:
            addrs = netifaces.ifaddresses(iface)
        except (ValueError, OSError):
            continue
        for family in (netifaces.AF_INET, netifaces.AF_INET6):
            if family not in addrs:
                continue
            for item in addrs[family]:
                addr = item.get("addr")
                if addr:
                    _add_ip_string(out, addr)
    return out


def _gather_interface_ips_socket() -> set[str]:
    """
    Резервный сбор адресов через stdlib (если netifaces недоступен).

    Включает IP, к которым разрешается hostname машины, и адрес интерфейса,
    выбранного для исходящего UDP (типичный «основной» адрес).
    """
    out: set[str] = set()
    try:
        hostname = socket.gethostname()
        _, _, ips = socket.gethostbyname_ex(hostname)
        for ip in ips:
            _add_ip_string(out, ip)
    except (OSError, socket.herror) as exc:
        logger.debug("gethostbyname_ex failed: %s", exc)

    for fam, _, _, _, sockaddr in socket.getaddrinfo(
        socket.gethostname(),
        None,
        type=socket.SOCK_STREAM,
    ):
        if fam == socket.AF_INET:
            _add_ip_string(out, sockaddr[0])
        elif fam == socket.AF_INET6:
            _add_ip_string(out, sockaddr[0])

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("203.0.113.1", 80))
        _add_ip_string(out, probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    return out


def _gather_interface_ips() -> set[str]:
    try:
        return _gather_interface_ips_netifaces()
    except ImportError:
        logger.debug("netifaces not installed; using socket-based interface address discovery")
        return _gather_interface_ips_socket()


def _gather_local_hosts() -> FrozenSet[str]:
    bucket: set[str] = set()

    bucket.add("localhost")
    bucket.add(_normalize_token("127.0.0.1"))
    bucket.add(_normalize_token("::1"))

    try:
        hn = socket.gethostname()
        if hn:
            bucket.add(hn.strip().lower())
    except OSError as exc:
        logger.warning("socket.gethostname() failed: %s", exc)

    bucket |= _gather_interface_ips()

    return frozenset(bucket)


def _ensure_cache() -> None:
    global _local_hosts
    if _local_hosts is not None:
        return
    with _lock:
        if _local_hosts is not None:
            return
        _local_hosts = _gather_local_hosts()
        logger.debug("host_resolver cache: %d entries", len(_local_hosts))


def init_host_resolver() -> None:
    """
    Предварительно заполняет кэш локальных имён и адресов.

    Вызывать при старте приложения опционально; иначе кэш заполнится при первом
    вызове :func:`is_local`.
    """
    _ensure_cache()


def is_local(hostname: str) -> bool:
    """
    Возвращает ``True``, если ``hostname`` указывает на эту машину.

    Локальным считается хост, если имя совпадает с ``localhost``, ``127.0.0.1``,
    ``::1``, результатом :func:`socket.gethostname` (без учёта регистра) или
    любым IP-адресом сетевых интерфейсов (IPv4/IPv6, с нормализацией формата).

    Список адресов интерфейсов предпочтительно строится через ``netifaces``;
    при его отсутствии используется перечисление на базе :mod:`socket`.
    """
    _ensure_cache()
    assert _local_hosts is not None

    token = hostname.strip()
    if not token:
        return False

    try:
        normalized = str(ipaddress.ip_address(token))
    except ValueError:
        normalized = token.lower()

    if normalized in _local_hosts:
        return True
    if token in _local_hosts:
        return True
    return False
