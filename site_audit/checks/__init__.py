"""
Пакет проверок.
"""

from . import (
    broken_links,
    duplicates,
    empty_pages,
    heading_structure,
    images,
    meta_quality,
    mixed_content,
    orphan_pages,
    placeholders,
    robots_sitemap,
    seo,
)

ALL_CHECKS = {
    empty_pages.CHECK_NAME: empty_pages,
    seo.CHECK_NAME: seo,
    broken_links.CHECK_NAME: broken_links,
    images.CHECK_NAME: images,
    duplicates.CHECK_NAME: duplicates,
    placeholders.CHECK_NAME: placeholders,
    robots_sitemap.CHECK_NAME: robots_sitemap,
    mixed_content.CHECK_NAME: mixed_content,
    orphan_pages.CHECK_NAME: orphan_pages,
    meta_quality.CHECK_NAME: meta_quality,
    heading_structure.CHECK_NAME: heading_structure,
}

__all__ = [
    "empty_pages",
    "seo",
    "broken_links",
    "images",
    "duplicates",
    "placeholders",
    "robots_sitemap",
    "mixed_content",
    "orphan_pages",
    "meta_quality",
    "heading_structure",
    "ALL_CHECKS",
]
