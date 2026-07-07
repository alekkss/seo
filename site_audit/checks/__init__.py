# site_audit/checks/__init__.py
"""
Пакет проверок (без security).
"""

from . import (
    empty_pages,
    seo,
    broken_links,
    images,
    redirects,
    duplicates,
    placeholders,
)

ALL_CHECKS = {
    empty_pages.CHECK_NAME: empty_pages,
    seo.CHECK_NAME: seo,
    broken_links.CHECK_NAME: broken_links,
    images.CHECK_NAME: images,
    redirects.CHECK_NAME: redirects,
    duplicates.CHECK_NAME: duplicates,
    placeholders.CHECK_NAME: placeholders,
}

__all__ = [
    "empty_pages",
    "seo",
    "broken_links",
    "images",
    "redirects",
    "duplicates",
    "placeholders",
    "ALL_CHECKS",
]
