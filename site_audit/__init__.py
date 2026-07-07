"""
site_audit — комплексный аудит сайта.

CLI-инструмент и Telegram-бот для автоматического поиска
технических проблем на сайте с генерацией отчётов в Excel и HTML.

Использование из командной строки:
    python -m site_audit https://example.com
    python -m site_audit https://example.com --checks seo,empty_pages --limit 50

Запуск Telegram-бота:
    python -m site_audit.bot

Использование как библиотеки:
    from site_audit.config import get_settings, get_logger, setup_logging
    from site_audit.services import AuditService
    from site_audit.crawler import try_sitemap, crawl
    from site_audit.checks import seo, empty_pages
    from site_audit.report import save_all
"""

__version__ = "1.1.0"
__author__ = "site_audit"
