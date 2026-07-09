"""
Проверка: качество мета-тегов title и description.

Что проверяется:
  - Title: слишком короткий (<30 символов), слишком длинный (>60 символов)
  - Title: начинается с домена или «Главная»
  - Title: keyword stuffing (одно слово повторяется 3+ раз)
  - Description: слишком короткая (<70 символов), слишком длинная (>160 символов)
  - Description: совпадает с title (копипаст)

Пороги длины основаны на рекомендациях Google:
  - Title отображается в SERP до ~60 символов (на десктопе)
  - Description — до ~160 символов (на десктопе)

Проверка CPU-bound: работает только с уже загруженным HTML.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from ..utils import AsyncResponse, is_html_response, parse_html

CHECK_NAME = "meta_quality"
DESCRIPTION = "Качество title и description"

# ── Пороги длины ────────────────────────────────────────────────────────────

TITLE_MIN_LENGTH: int = 30
TITLE_MAX_LENGTH: int = 60
DESC_MIN_LENGTH: int = 70
DESC_MAX_LENGTH: int = 160

# ── Порог keyword stuffing: слово повторяется N+ раз в title ────────────────

_KEYWORD_STUFFING_THRESHOLD: int = 3

# Минимальная длина слова для учёта в keyword stuffing (исключаем предлоги)
_MIN_WORD_LENGTH: int = 4

# ── Стоп-слова в начале title (бесполезные, не несут смысла) ────────────────

_TITLE_BAD_STARTS: tuple[str, ...] = (
    "главная",
    "home",
    "untitled",
    "без названия",
    "новая страница",
    "new page",
    "страница",
)

# Регулярное выражение для извлечения слов (кириллица + латиница)
_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ]+", re.UNICODE)


# ═════════════════════════════════════════════════════════════════════════════
# Проверка одной страницы
# ═════════════════════════════════════════════════════════════════════════════

def _check_single_page(
    url: str,
    html: str,
) -> list[dict[str, Any]]:
    """
    Проверяет качество title и description на одной странице.

    Args:
        url: URL страницы.
        html: HTML-код страницы.

    Returns:
        Список найденных проблем.
    """
    soup = parse_html(html)
    issues: list[dict[str, Any]] = []

    # ── Извлекаем title ─────────────────────────────────────────────────
    title_tag = soup.title
    title = title_tag.string.strip() if (title_tag and title_tag.string) else ""

    # ── Извлекаем description ───────────────────────────────────────────
    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = ""
    if desc_tag and desc_tag.get("content"):
        description = desc_tag["content"].strip()

    # ── Проверки title ──────────────────────────────────────────────────
    if title:
        title_len = len(title)

        if title_len < TITLE_MIN_LENGTH:
            issues.append(_make_issue(
                url=url,
                field="title",
                issue_type="too_short",
                message=(
                    f"Title слишком короткий: {title_len} символов "
                    f"(минимум {TITLE_MIN_LENGTH})"
                ),
                value=title,
                length=title_len,
            ))

        if title_len > TITLE_MAX_LENGTH:
            issues.append(_make_issue(
                url=url,
                field="title",
                issue_type="too_long",
                message=(
                    f"Title слишком длинный: {title_len} символов "
                    f"(максимум {TITLE_MAX_LENGTH}, обрежется в SERP)"
                ),
                value=title,
                length=title_len,
            ))

        # Проверка бесполезного начала title
        title_lower = title.lower().strip()
        for bad_start in _TITLE_BAD_STARTS:
            if title_lower.startswith(bad_start):
                issues.append(_make_issue(
                    url=url,
                    field="title",
                    issue_type="bad_start",
                    message=f"Title начинается с «{bad_start}» — неинформативно",
                    value=title,
                    length=title_len,
                ))
                break

        # Проверка keyword stuffing
        stuffed_word = _detect_keyword_stuffing(title)
        if stuffed_word:
            issues.append(_make_issue(
                url=url,
                field="title",
                issue_type="keyword_stuffing",
                message=(
                    f"Keyword stuffing в title: слово «{stuffed_word}» "
                    f"повторяется {_count_word(title, stuffed_word)} раз"
                ),
                value=title,
                length=title_len,
            ))

    # ── Проверки description ────────────────────────────────────────────
    if description:
        desc_len = len(description)

        if desc_len < DESC_MIN_LENGTH:
            issues.append(_make_issue(
                url=url,
                field="description",
                issue_type="too_short",
                message=(
                    f"Description слишком короткая: {desc_len} символов "
                    f"(минимум {DESC_MIN_LENGTH})"
                ),
                value=description,
                length=desc_len,
            ))

        if desc_len > DESC_MAX_LENGTH:
            issues.append(_make_issue(
                url=url,
                field="description",
                issue_type="too_long",
                message=(
                    f"Description слишком длинная: {desc_len} символов "
                    f"(максимум {DESC_MAX_LENGTH}, обрежется в SERP)"
                ),
                value=description,
                length=desc_len,
            ))

    # ── Description совпадает с title ───────────────────────────────────
    if title and description:
        if _normalize_text(title) == _normalize_text(description):
            issues.append(_make_issue(
                url=url,
                field="description",
                issue_type="same_as_title",
                message="Description совпадает с title — нет дополнительной информации",
                value=description,
                length=len(description),
            ))

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ═════════════════════════════════════════════════════════════════════════════

def _make_issue(
    *,
    url: str,
    field: str,
    issue_type: str,
    message: str,
    value: str,
    length: int,
) -> dict[str, Any]:
    """
    Формирует словарь проблемы в едином формате.

    Args:
        url: URL страницы.
        field: поле проверки ('title' или 'description').
        issue_type: тип проблемы.
        message: описание проблемы.
        value: значение поля.
        length: длина значения.

    Returns:
        Словарь проблемы.
    """
    return {
        "check": CHECK_NAME,
        "url": url,
        "field": field,
        "issue_type": issue_type,
        "message": message,
        "value": value,
        "length": length,
    }


def _normalize_text(text: str) -> str:
    """Нормализует текст для сравнения: нижний регистр, убирает лишние пробелы."""
    return " ".join(text.lower().split())


def _detect_keyword_stuffing(title: str) -> str:
    """
    Находит слово, которое повторяется в title слишком часто.

    Args:
        title: текст title.

    Returns:
        Повторяющееся слово или пустая строка, если stuffing не найден.
    """
    words = _WORD_RE.findall(title.lower())
    # Учитываем только слова длиннее порога (исключаем предлоги, союзы)
    meaningful = [w for w in words if len(w) >= _MIN_WORD_LENGTH]

    if not meaningful:
        return ""

    counter = Counter(meaningful)
    most_common_word, count = counter.most_common(1)[0]

    if count >= _KEYWORD_STUFFING_THRESHOLD:
        return most_common_word

    return ""


def _count_word(text: str, word: str) -> int:
    """Считает количество вхождений слова в текст (без учёта регистра)."""
    words = _WORD_RE.findall(text.lower())
    return sum(1 for w in words if w == word)


# ═════════════════════════════════════════════════════════════════════════════
# Пакетная проверка
# ═════════════════════════════════════════════════════════════════════════════

def check_many(
    pages: list[dict[str, Any]],
    *,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Проверяет качество мета-тегов на всех страницах.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с проблемами мета-тегов.
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

def filter_title_issues(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только проблемы title."""
    return [r for r in results if r["field"] == "title"]


def filter_desc_issues(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только проблемы description."""
    return [r for r in results if r["field"] == "description"]


def filter_by_type(
    results: list[dict[str, Any]],
    issue_type: str,
) -> list[dict[str, Any]]:
    """Возвращает проблемы конкретного типа."""
    return [r for r in results if r["issue_type"] == issue_type]


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

_ISSUE_TYPE_LABELS: dict[str, str] = {
    "too_short": "Слишком короткий",
    "too_long": "Слишком длинный",
    "bad_start": "Неинформативное начало",
    "keyword_stuffing": "Keyword stuffing",
    "same_as_title": "Совпадает с title",
}


def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    title_issues = filter_title_issues(results)
    desc_issues = filter_desc_issues(results)

    lines = [
        f"[{CHECK_NAME}] Найдено: {len(results)} "
        f"(title: {len(title_issues)}, description: {len(desc_issues)})"
    ]

    # Группируем по типу проблемы для компактности
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        key = f"{r['field']}:{r['issue_type']}"
        by_type[key].append(r)

    for key, items in by_type.items():
        field, issue_type = key.split(":", 1)
        label = _ISSUE_TYPE_LABELS.get(issue_type, issue_type)
        field_label = "Title" if field == "title" else "Description"
        lines.append(f"  {field_label} — {label} ({len(items)} шт.):")
        for item in items[:10]:
            value_short = item["value"][:60]
            if len(item["value"]) > 60:
                value_short += "..."
            lines.append(
                f"    ✗ {item['url']}"
            )
            lines.append(
                f"        «{value_short}» ({item['length']} симв.)"
            )
        if len(items) > 10:
            lines.append(f"        ...и ещё {len(items) - 10}")

    return "\n".join(lines)
