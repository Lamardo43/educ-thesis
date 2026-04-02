"""
Общие (shared) модели ответов, переиспользуемые во всех роутерах.

Вынесены в отдельный модуль для устранения дублирования
определений MessageResponse, DashboardSummary и т.д. в роутерах.
"""

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    """Стандартный ответ с текстовым сообщением."""

    message: str


class DashboardSummary(BaseModel):
    """
    Сводная статистика для главной панели управления.

    Отображается в четырёх карточках вверху Dashboard:
    всего заглушек, активных, с ошибками, доступных хостов.
    """

    total_mocks: int = Field(0, description="Общее число зарегистрированных заглушек")
    running_mocks: int = Field(0, description="Число заглушек в статусе RUNNING")
    error_mocks: int = Field(0, description="Число заглушек в статусе ERROR")
    available_hosts: int = Field(0, description="Число хостов в статусе AVAILABLE")
    total_hosts: int = Field(0, description="Общее число зарегистрированных хостов")


class ErrorDetail(BaseModel):
    """Структурированный ответ об ошибке."""

    detail: str
    code: str = Field("", description="Машиночитаемый код ошибки")
