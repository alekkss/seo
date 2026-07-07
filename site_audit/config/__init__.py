"""
Пакет конфигурации приложения.

Централизованный доступ к настройкам и логированию:
    from site_audit.config import settings, get_logger
"""

from .settings import Settings, get_settings
from .logger import get_logger, setup_logging

__all__ = [
    "Settings",
    "get_settings",
    "get_logger",
    "setup_logging",
]
