"""
Проверка: структура заголовков H1–H6.

Что проверяется:
  - Множественные H1 на одной странице
  - Отсутствие H1
  - Пропущенные уровни заголовков (H1 → H3 без H2)
  - Пустые заголовки (тег есть, текста нет)
  - Слишком длинные заголовки (>100 символов)
  - Первый заголовок на странице не H1

Правильная иерархия заголовков важна для:
  - SEO: поисковики используют H1 как основной сигнал тематики страницы
  - Accessibility: скринридеры строят навигацию по заголовкам
  - UX: логичная структура помогает пользователям сканировать контент

Проверка CPU-bound: работает только с уже загруженным HTML.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..utils import AsyncResponse, is_html_response, parse_html

CHECK_NAME = "heading_structure"
DESCRIPTION = "Структура заголовков H1–H6"

# Максимальная допустимая длина текста заголовка
_MAX_HEADING_LENGTH: int = 100

# Все уровни заголовков в порядке иерархии
_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")


# ═════════════════════════════════════════════════════════════════════════════
# Проверка одной страницы
# ═════════════════════════════════════════════════════════════════════════════

def _check_single_page(
    url: str,
    html: str,
) -> list[dict[str, Any]]:
    """
    Проверяет структуру заголовков на одной странице.

    Args:
        url: URL страницы.
        html: HTML-код страницы.

    Returns:
        Список найденных проблем.
    """
    soup = parse_html(html)
    issues: list[dict[str, Any]] = []

    # Собираем все заголовки в порядке появления в DOM
    headings: list[dict[str, Any]] = []
    for tag in soup.find_all(_HEADING_TAGS):
        text = tag.get_text(strip=True)
        level = int(tag.name[1])
        headings.append({
            "tag": tag.name,
            "level": level,
            "text": text,
        })

    # ── Отсутствие H1 ──────────────────────────────────────────────────
    h1_list = [h for h in headings if h["level"] == 1]

    if not h1_list:
        issues.append(_make_issue(
            url=url,
            issue_type="missing_h1",
            message="На странице отсутствует H1",
            tag="",
            heading_text="",
        ))

    # ── Множественные H1 ───────────────────────────────────────────────
    if len(h1_list) > 1:
        h1_texts = [h["text"][:80] for h in h1_list]
        issues.append(_make_issue(
            url=url,
            issue_type="multiple_h1",
            message=f"На странице {len(h1_list)} заголовков H1 (рекомендуется 1)",
            tag="h1",
            heading_text=" | ".join(h1_texts),
        ))

    # ── Первый заголовок не H1 ──────────────────────────────────────────
    if headings and headings[0]["level"] != 1:
        first = headings[0]
        issues.append(_make_issue(
            url=url,
            issue_type="first_not_h1",
            message=(
                f"Первый заголовок на странице — {first['tag'].upper()}, "
                f"а не H1"
            ),
            tag=first["tag"],
            heading_text=first["text"][:80],
        ))

    # ── Пропущенные уровни ──────────────────────────────────────────────
    if len(headings) >= 2:
        for i in range(1, len(headings)):
            prev_level = headings[i - 1]["level"]
            curr_level = headings[i]["level"]

            # Допустимо: тот же уровень, уровень выше (меньше числом),
            # или следующий уровень (+1). Пропуск — это +2 и больше.
            if curr_level > prev_level + 1:
                skipped_from = f"H{prev_level}"
                skipped_to = f"H{curr_level}"
                # Формируем список пропущенных уровней
                missing_levels = [
                    f"H{lvl}"
                    for lvl in range(prev_level + 1, curr_level)
                ]
                issues.append(_make_issue(
                    url=url,
                    issue_type="skipped_level",
                    message=(
                        f"Пропущен уровень: {skipped_from} → {skipped_to} "
                        f"(нет {', '.join(missing_levels)})"
                    ),
                    tag=headings[i]["tag"],
                    heading_text=headings[i]["text"][:80],
                ))

    # ── Пустые заголовки ────────────────────────────────────────────────
    for heading in headings:
        if not heading["text"]:
            issues.append(_make_issue(
                url=url,
                issue_type="empty_heading",
                message=f"Пустой заголовок {heading['tag'].upper()} (нет текста)",
                tag=heading["tag"],
                heading_text="",
            ))

    # ── Слишком длинные заголовки ───────────────────────────────────────
    for heading in headings:
        text_len = len(heading["text"])
        if text_len > _MAX_HEADING_LENGTH:
            issues.append(_make_issue(
                url=url,
                issue_type="too_long",
                message=(
                    f"Заголовок {heading['tag'].upper()} слишком длинный: "
                    f"{text_len} символов (максимум {_MAX_HEADING_LENGTH})"
                ),
                tag=heading["tag"],
                heading_text=heading["text"][:80] + "...",
            ))

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ═════════════════════════════════════════════════════════════════════════════

def _make_issue(
    *,
    url: str,
    issue_type: str,
    message: str,
    tag: str,
    heading_text: str,
) -> dict[str, Any]:
    """
    Формирует словарь проблемы в едином формате.

    Args:
        url: URL страницы.
        issue_type: тип проблемы.
        message: описание проблемы.
        tag: HTML-тег заголовка (h1–h6).
        heading_text: текст заголовка.

    Returns:
        Словарь проблемы.
    """
    return {
        "check": CHECK_NAME,
        "url": url,
        "issue_type": issue_type,
        "message": message,
        "tag": tag,
        "heading_text": heading_text,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Пакетная проверка
# ═════════════════════════════════════════════════════════════════════════════

def check_many(
    pages: list[dict[str, Any]],
    *,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Проверяет структуру заголовков на всех страницах.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с проблемами заголовков.
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

def filter_by_type(
    results: list[dict[str, Any]],
    issue_type: str,
) -> list[dict[str, Any]]:
    """Возвращает проблемы конкретного типа."""
    return [r for r in results if r["issue_type"] == issue_type]


def filter_missing_h1(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает страницы без H1."""
    return filter_by_type(results, "missing_h1")


def filter_multiple_h1(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает страницы с множественными H1."""
    return filter_by_type(results, "multiple_h1")


def filter_skipped_levels(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает проблемы с пропущенными уровнями."""
    return filter_by_type(results, "skipped_level")


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

_ISSUE_TYPE_LABELS: dict[str, str] = {
    "missing_h1": "Отсутствует H1",
    "multiple_h1": "Множественные H1",
    "first_not_h1": "Первый заголовок не H1",
    "skipped_level": "Пропущен уровень",
    "empty_heading": "Пустой заголовок",
    "too_long": "Слишком длинный заголовок",
}


def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    lines = [f"[{CHECK_NAME}] Найдено проблем: {len(results)}"]

    # Группируем по типу проблемы
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_type[r["issue_type"]].append(r)

    for issue_type, label in _ISSUE_TYPE_LABELS.items():
        items = by_type.get(issue_type, [])
        if not items:
            continue

        lines.append(f"  {label} ({len(items)} шт.):")

        for item in items[:10]:
            lines.append(f"    ✗ {item['url']}")
            if item["heading_text"]:
                text_short = item["heading_text"][:70]
                if len(item["heading_text"]) > 70:
                    text_short += "..."
                lines.append(f"        «{text_short}»")
            if item["message"] and item["issue_type"] == "skipped_level":
                lines.append(f"        {item['message']}")

        if len(items) > 10:
            lines.append(f"        ...и ещё {len(items) - 10}")

    return "\n".join(lines)
