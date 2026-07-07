"""
Пакет Telegram-бота для запуска аудита сайта.

Использование:
    from site_audit.bot import run_bot

    run_bot()  # запуск polling
"""

from .app import run_bot

__all__ = [
    "run_bot",
]
