# site_audit/__init__.py
"""
site_audit — комплексный аудит сайта.

Использование из командной строки:
    python -m site_audit https://example.com
    python -m site_audit https://example.com --checks seo,empty_pages --limit 50

Использование как библиотеки:
    from site_audit.crawler import try_sitemap, crawl
    from site_audit.checks import seo, empty_pages
    from site_audit.report import save_all
"""

__version__ = "1.0.0"
__author__ = "site_audit"
