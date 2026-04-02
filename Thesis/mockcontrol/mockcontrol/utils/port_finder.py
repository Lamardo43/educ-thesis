"""
Утилита для работы с TCP-портами.

Предоставляет функции валидации диапазонов и парсинга
вывода ss (socket statistics) для поиска свободных портов.
"""

import re
from typing import Optional


def validate_port_range(port_min: int, port_max: int) -> Optional[str]:
    """
    Проверить корректность диапазона портов.

    Returns:
        None если диапазон валиден, строка ошибки если нет.
    """
    if port_min < 1024:
        return f"port_min ({port_min}) must be >= 1024"
    if port_max > 65535:
        return f"port_max ({port_max}) must be <= 65535"
    if port_min > port_max:
        return f"port_min ({port_min}) must be <= port_max ({port_max})"
    return None


def parse_occupied_ports(ss_output: str) -> set[int]:
    """
    Извлечь занятые TCP-порты из вывода команды ``ss -tlnH``.

    Парсит строки вида::

        LISTEN  0  128  0.0.0.0:8100  0.0.0.0:*
        LISTEN  0  128  [::]:22       [::]:*

    Returns:
        Множество занятых портов.
    """
    occupied: set[int] = set()
    # Ищем порт в четвёртом столбце (Local Address:Port)
    for line in ss_output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            addr_port = parts[3]
            # Извлечь порт после последнего двоеточия
            match = re.search(r":(\d+)$", addr_port)
            if match:
                try:
                    occupied.add(int(match.group(1)))
                except ValueError:
                    continue
    return occupied


def find_free_in_range(
    occupied: set[int],
    port_min: int,
    port_max: int,
) -> Optional[int]:
    """
    Найти первый свободный порт в заданном диапазоне.

    Args:
        occupied: Множество занятых портов.
        port_min: Нижняя граница диапазона (включительно).
        port_max: Верхняя граница диапазона (включительно).

    Returns:
        Номер свободного порта или None, если все заняты.
    """
    for port in range(port_min, port_max + 1):
        if port not in occupied:
            return port
    return None
