"""
Пакет веб-интерфейса MockControl (Jinja2 SSR).

Содержит:
- routes.py     — HTML-роуты для Dashboard, Settings, Logs
- templates/    — Jinja2-шаблоны (base, dashboard, settings, logs)
- static/       — CSS и JavaScript
"""

from mockcontrol.web.routes import router as web_router

__all__ = ["web_router"]
