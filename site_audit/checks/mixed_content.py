"""
Проверка: Mixed Content — HTTP-ресурсы на HTTPS-страницах.

Что проверяется:
  - Изображения (<img src>, <img srcset>)
  - Скрипты (<script src>)
  - Стили (<link rel="stylesheet" href>)
  - Фреймы (<iframe src>)
  - Медиа (<video src>, <audio src>, <source src>)
  - Фоновые изображения в inline-стилях (style="background: url(http://...)")

Браузеры блокируют «активный» mixed content (скрипты, стили, iframe)
и показывают предупреждение для «пассивного» (изображения, видео).
Оба варианта — проблема для безопасности и доверия пользователей.

Проверка CPU-bound: работает только с уже загруженным HTML.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from ..utils import AsyncResponse, is_html_response, parse_html

CHECK_NAME = "mixed_content"
DESCRIPTION = "HTTP-ресурсы на HTTPS-страницах (Mixed Content)"

# Типы mixed content: активный блокируется браузером, пассивный — предупреждение
_ACTIVE_CONTENT_LABEL = "активный (блокируется браузером)"
_PASSIVE_CONTENT_LABEL = "пассивный (предупреждение)"

# Теги и атрибуты для проверки.
# Формат: (css_selector, атрибут, тип_контента)
_RESOURCE_TAGS: list[tuple[str, str, str]] = [
    ("script[src]", "src", _ACTIVE_CONTENT_LABEL),
    ("link[rel=stylesheet][href]", "href", _ACTIVE_CONTENT_LABEL),
    ("iframe[src]", "src", _ACTIVE_CONTENT_LABEL),
    ("object[data]", "data", _ACTIVE_CONTENT_LABEL),
    ("img[src]", "src", _PASSIVE_CONTENT_LABEL),
    ("video[src]", "src", _PASSIVE_CONTENT_LABEL),
    ("audio[src]", "src", _PASSIVE_CONTENT_LABEL),
    ("source[src]", "src", _PASSIVE_CONTENT_LABEL),
    ("video[poster]", "poster", _PASSIVE_CONTENT_LABEL),
]

# Регулярное выражение для поиска url(...) в inline-стилях
_INLINE_STYLE_URL_RE = re.compile(
    r"""url\(\s*['"]?(http://[^'")]+)['"]?\s*\)""",
    re.IGNORECASE,
)


# ═════════════════════════════════════════════════════════════════════════════
# Проверка одной страницы
# ═════════════════════════════════════════════════════════════════════════════

def _check_single_page(
    url: str,
    html: str,
) -> list[dict[str, Any]]:
    """
    Ищет HTTP-ресурсы на одной HTTPS-странице.

    Args:
        url: URL страницы.
        html: HTML-код страницы.

    Returns:
        Список найденных проблем mixed content.
    """
    # Проверяем только HTTPS-страницы
    if not url.startswith("https://"):
        return []

    soup = parse_html(html)
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    # ── Проверка тегов с атрибутами ─────────────────────────────────────
    for selector, attr, content_type in _RESOURCE_TAGS:
        for tag in soup.select(selector):
            value = tag.get(attr, "").strip()

            if not value:
                continue

            if not value.startswith("http://"):
                continue

            # Дедупликация в пределах одной страницы
            dedup_key = f"{attr}:{value}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            issues.append({
                "check": CHECK_NAME,
                "page_url": url,
                "resource_url": value,
                "tag": tag.name,
                "attribute": attr,
                "content_type": content_type,
            })

    # ── Проверка srcset у <img> ─────────────────────────────────────────
    for img in soup.find_all("img", srcset=True):
        srcset = img["srcset"]
        # srcset содержит кандидатов через запятую: "url1 1x, url2 2x"
        for candidate in srcset.split(","):
            candidate_url = candidate.strip().split()[0] if candidate.strip() else ""

            if not candidate_url.startswith("http://"):
                continue

            dedup_key = f"srcset:{candidate_url}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            issues.append({
                "check": CHECK_NAME,
                "page_url": url,
                "resource_url": candidate_url,
                "tag": "img",
                "attribute": "srcset",
                "content_type": _PASSIVE_CONTENT_LABEL,
            })

    # ── Проверка inline-стилей (style="background: url(http://...)") ────
    for tag in soup.find_all(style=True):
        style_value = tag["style"]
        for match in _INLINE_STYLE_URL_RE.finditer(style_value):
            resource_url = match.group(1).strip()

            dedup_key = f"style:{resource_url}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            issues.append({
                "check": CHECK_NAME,
                "page_url": url,
                "resource_url": resource_url,
                "tag": tag.name,
                "attribute": "style",
                "content_type": _ACTIVE_CONTENT_LABEL,
            })

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# Пакетная проверка
# ═════════════════════════════════════════════════════════════════════════════

def check_many(
    pages: list[dict[str, Any]],
    *,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Проверяет все страницы на наличие mixed content.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с проблемами mixed content.
    """
    if verbose:
        print(f"  [{CHECK_NAME}] Проверяю {len(pages)} страниц...")

    results: list[dict[str, Any]] = []

    for page in pages:
        url = page["url"]
        html = page.get("html")

        if html is None:
            resp = page.get("resp")
            if resp is None or isinstance(resp, Exception):
                continue
            if isinstance(resp, AsyncResponse):
                if not resp.ok or resp.status != 200:
                    continue
                if not is_html_response(resp):
                    continue
                html = resp.text
            else:
                if not is_html_response(resp):
                    continue
                html = resp.text

        page_issues = _check_single_page(url, html)
        results.extend(page_issues)

    if verbose:
        print(f"  [{CHECK_NAME}] Найдено проблем: {len(results)}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Фильтры
# ═════════════════════════════════════════════════════════════════════════════

def filter_active(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только активный mixed content (блокируется браузером)."""
    return [r for r in results if r["content_type"] == _ACTIVE_CONTENT_LABEL]


def filter_passive(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только пассивный mixed content (предупреждение)."""
    return [r for r in results if r["content_type"] == _PASSIVE_CONTENT_LABEL]


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    active = filter_active(results)
    passive = filter_passive(results)

    lines = [
        f"[{CHECK_NAME}] Найдено: {len(results)} "
        f"(активных {len(active)}, пассивных {len(passive)})"
    ]

    if active:
        lines.append("  Активный (блокируется браузером):")
        # Группируем по странице для компактности
        by_page: dict[str, list[str]] = defaultdict(list)
        for r in active:
            by_page[r["page_url"]].append(
                f"<{r['tag']} {r['attribute']}>: {r['resource_url']}"
            )
        for page_url, resources in by_page.items():
            lines.append(f"    ✗ {page_url}")
            for res in resources:
                lines.append(f"        {res}")

    if passive:
        lines.append("  Пассивный (предупреждение):")
        by_page_p: dict[str, list[str]] = defaultdict(list)
        for r in passive:
            by_page_p[r["page_url"]].append(
                f"<{r['tag']} {r['attribute']}>: {r['resource_url']}"
            )
        for page_url, resources in by_page_p.items():
            lines.append(f"    ⚠ {page_url}")
            for res in resources:
                lines.append(f"        {res}")

    return "\n".join(lines)
