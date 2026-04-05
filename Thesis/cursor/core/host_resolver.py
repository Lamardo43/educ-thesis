"""Определение, относится ли целевой хост к той же машине, что и MockControl.

Список локальных имён и IP собирается один раз при загрузке модуля. Результаты
`is_local(hostname)` дополнительно кэшируются через `functools.lru_cache`.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import socket
from typing import Final

logger = logging.getLogger(__name__)


def _register_address_or_name(value: str, ips: set[str], names_lower: set[str]) -> None:
    """Классифицирует строку как IP (нормализованный) или как имя хоста (в нижнем регистре)."""
    v = value.strip()
    if not v:
        return
    try:
        ips.add(str(ipaddress.ip_address(v)))
    except ValueError:
        names_lower.add(v.lower())


def _collect_local_identifiers() -> tuple[frozenset[str], frozenset[str]]:
    """Один раз собирает множества нормализованных IP и имён хостов (lower-case)."""
    ips: set[str] = set()
    names_lower: set[str] = set()

    for literal in ("localhost", "127.0.0.1", "::1"):
        _register_address_or_name(literal, ips, names_lower)

    try:
        hn = socket.gethostname()
    except OSError as exc:
        logger.warning("socket.gethostname() unavailable: %s", exc)
        hn = ""

    if hn:
        _register_address_or_name(hn, ips, names_lower)
        names_lower.add(hn.lower())
        try:
            _, _, ip_list = socket.gethostbyname_ex(hn)
            for ip in ip_list:
                _register_address_or_name(ip, ips, names_lower)
        except OSError:
            pass
        try:
            for res in socket.getaddrinfo(hn, None, type=socket.SOCK_STREAM):
                addr = res[4][0]
                _register_address_or_name(addr, ips, names_lower)
        except OSError:
            pass

    try:
        for res in socket.getaddrinfo("localhost", None, type=socket.SOCK_STREAM):
            addr = res[4][0]
            _register_address_or_name(addr, ips, names_lower)
    except OSError:
        pass

    # Типичный адрес маршрута по умолчанию (ещё один локальный IP интерфейса)
    for family, connect_addr in (
        (socket.AF_INET, ("8.8.8.8", 80)),
        (socket.AF_INET6, ("2001:4860:4860::8888", 80)),
    ):
        try:
            probe = socket.socket(family, socket.SOCK_DGRAM)
            try:
                probe.connect(connect_addr)
                local = probe.getsockname()[0]
                _register_address_or_name(local, ips, names_lower)
            finally:
                probe.close()
        except OSError:
            pass

    return frozenset(ips), frozenset(names_lower)


_LOCAL_IPS: Final[frozenset[str]]
_LOCAL_NAMES_LOWER: Final[frozenset[str]]
_LOCAL_IPS, _LOCAL_NAMES_LOWER = _collect_local_identifiers()


@functools.lru_cache(maxsize=None)
def is_local(hostname: str) -> bool:
    """Возвращает True, если `hostname` указывает на эту машину (кэш по аргументу)."""
    h = hostname.strip()
    if not h:
        return False
    if h.lower() in _LOCAL_NAMES_LOWER:
        return True
    try:
        return str(ipaddress.ip_address(h)) in _LOCAL_IPS
    except ValueError:
        return False
