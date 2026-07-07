"""
Пакет проверок.
"""

from . import (
    empty_pages,
    seo,
    broken_links,
    images,
    duplicates,
    placeholders,
)

ALL_CHECKS = {
    empty_pages.CHECK_NAME: empty_pages,
    seo.CHECK_NAME: seo,
    broken_links.CHECK_NAME: broken_links,
    images.CHECK_NAME: images,
    duplicates.CHECK_NAME: duplicates,
    placeholders.CHECK_NAME: placeholders,
}

__all__ = [
    "empty_pages",
    "seo",
    "broken_links",
    "images",
    "duplicates",
    "placeholders",
    "ALL_CHECKS",
]
