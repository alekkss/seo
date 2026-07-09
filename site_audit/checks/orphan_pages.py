"""
Проверка: страницы-сироты (Orphan Pages).

Что проверяется:
  - Страницы, которые присутствуют в sitemap (или в списке проверяемых URL),
    но на которые не ведёт ни одна внутренняя ссылка с других страниц сайта.

Почему это важно:
  - Поисковики находят такие страницы через sitemap, но ранжируют их
    ниже из-за отсутствия внутренних ссылок (нет передачи PageRank).
  - Пользователи не могут добраться до этих страниц через навигацию.
  - Часто это забытые страницы, черновики или устаревший контент.

Алгоритм:
  1. Извлекает все внутренние ссылки со всех загруженных страниц.
  2. Строит множество «целей» — URL, на которые кто-то ссылается.
  3. Сравнивает с полным списком URL.
  4. Страницы без входящих ссылок (кроме главной) — сироты.

Проверка CPU-bound: работает только с уже загруженным HTML.
"""

from __future__ import annotations

from typing import Any

from ..crawler import extract_links
from ..utils import (
    AsyncResponse,
    get_domain,
    is_html_response,
    normalize_url,
)

CHECK_NAME = "orphan_pages"
DESCRIPTION = "Страницы-сироты (нет входящих внутренних ссылок)"


# ═════════════════════════════════════════════════════════════════════════════
# Сбор входящих ссылок
# ═════════════════════════════════════════════════════════════════════════════

def _collect_internal_link_targets(
    pages: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """
    Строит карту входящих внутренних ссылок: target_url → {source_url, ...}.

    Для каждой страницы извлекает все внутренние ссылки и запоминает,
    какие URL являются целями этих ссылок.

    Args:
        pages: список словарей {"url", "resp", "html"}.

    Returns:
        Словарь {нормализованный_target_url: {source_url_1, source_url_2, ...}}.
    """
    inbound: dict[str, set[str]] = {}

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

        try:
            links = extract_links(html, url)
        except Exception:
            continue

        for link in links["internal"]:
            norm = normalize_url(link)
            inbound.setdefault(norm, set()).add(url)

    return inbound


# ═════════════════════════════════════════════════════════════════════════════
# Основная проверка
# ═════════════════════════════════════════════════════════════════════════════

def check(
    pages: list[dict[str, Any]],
    *,
    all_urls: list[str] | None = None,
    sitemap_urls: set[str] | None = None,
    base_url: str = "",
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Находит страницы-сироты: URL без входящих внутренних ссылок.

    Логика определения сирот:
      - Берём полный список URL для проверки (all_urls или urls из pages).
      - Строим множество URL, на которые ведут внутренние ссылки.
      - Разница — сироты (кроме главной страницы).
      - Если передан sitemap_urls, дополнительно помечаем,
        находится ли сирота в sitemap.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        all_urls: полный список URL сайта (до limit).
            Если None — берётся из pages.
        sitemap_urls: множество URL из sitemap (до limit).
            Если передан — добавляется информация «в sitemap / не в sitemap».
        base_url: корневой URL сайта (главная страница исключается из сирот).
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о страницах-сиротах.
    """
    if verbose:
        print(f"  [{CHECK_NAME}] Анализирую внутреннюю перелинковку...")

    # ── Шаг 1: определяем полный список URL ─────────────────────────────
    if all_urls is not None:
        check_urls = all_urls
    else:
        check_urls = [page["url"] for page in pages]

    # Нормализуем все URL для корректного сравнения
    norm_check_urls: dict[str, str] = {}
    for url in check_urls:
        norm = normalize_url(url)
        # Сохраняем оригинальный URL для отчёта
        if norm not in norm_check_urls:
            norm_check_urls[norm] = url

    # ── Шаг 2: собираем входящие ссылки ─────────────────────────────────
    inbound = _collect_internal_link_targets(pages)

    # ── Шаг 3: нормализуем URL главной страницы для исключения ──────────
    excluded_norms: set[str] = set()
    if base_url:
        excluded_norms.add(normalize_url(base_url))
        # Также исключаем варианты с/без trailing slash
        excluded_norms.add(normalize_url(base_url.rstrip("/") + "/"))

    # ── Шаг 4: нормализуем sitemap_urls ─────────────────────────────────
    norm_sitemap: set[str] = set()
    if sitemap_urls:
        norm_sitemap = {normalize_url(u) for u in sitemap_urls}

    # ── Шаг 5: находим сирот ────────────────────────────────────────────
    results: list[dict[str, Any]] = []

    for norm_url, original_url in norm_check_urls.items():
        # Главную страницу не считаем сиротой
        if norm_url in excluded_norms:
            continue

        # Проверяем наличие входящих ссылок
        sources = inbound.get(norm_url, set())
        if sources:
            continue

        # Это сирота — формируем запись
        in_sitemap = norm_url in norm_sitemap if norm_sitemap else None

        results.append({
            "check": CHECK_NAME,
            "url": original_url,
            "inbound_links": 0,
            "in_sitemap": in_sitemap,
            "message": _format_message(in_sitemap),
        })

    if verbose:
        print(f"  [{CHECK_NAME}] Найдено страниц-сирот: {len(results)}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ═════════════════════════════════════════════════════════════════════════════

def _format_message(in_sitemap: bool | None) -> str:
    """
    Формирует описание проблемы.

    Args:
        in_sitemap: находится ли страница в sitemap (None — неизвестно).

    Returns:
        Текст описания.
    """
    base = "Нет входящих внутренних ссылок"
    if in_sitemap is True:
        return f"{base} (есть в sitemap — поисковик найдёт, но вес не получит)"
    if in_sitemap is False:
        return f"{base} (нет в sitemap — страница практически невидима)"
    return base


# ═════════════════════════════════════════════════════════════════════════════
# Фильтры
# ═════════════════════════════════════════════════════════════════════════════

def filter_in_sitemap(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает сирот, которые есть в sitemap."""
    return [r for r in results if r["in_sitemap"] is True]


def filter_not_in_sitemap(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает сирот, которых нет в sitemap."""
    return [r for r in results if r["in_sitemap"] is False]


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    in_sm = filter_in_sitemap(results)
    not_in_sm = filter_not_in_sitemap(results)

    parts: list[str] = []
    if in_sm:
        parts.append(f"в sitemap: {len(in_sm)}")
    if not_in_sm:
        parts.append(f"не в sitemap: {len(not_in_sm)}")

    detail = f" ({', '.join(parts)})" if parts else ""
    lines = [f"[{CHECK_NAME}] Страниц-сирот: {len(results)}{detail}"]

    if in_sm:
        lines.append("  Есть в sitemap, но нет входящих ссылок:")
        for r in in_sm[:15]:
            lines.append(f"    ⚠ {r['url']}")
        if len(in_sm) > 15:
            lines.append(f"        ...и ещё {len(in_sm) - 15}")

    if not_in_sm:
        lines.append("  Нет ни в sitemap, ни входящих ссылок:")
        for r in not_in_sm[:15]:
            lines.append(f"    ✗ {r['url']}")
        if len(not_in_sm) > 15:
            lines.append(f"        ...и ещё {len(not_in_sm) - 15}")

    # Сироты без информации о sitemap
    unknown = [r for r in results if r["in_sitemap"] is None]
    if unknown:
        lines.append("  Нет входящих ссылок:")
        for r in unknown[:15]:
            lines.append(f"    ✗ {r['url']}")
        if len(unknown) > 15:
            lines.append(f"        ...и ещё {len(unknown) - 15}")

    return "\n".join(lines)
