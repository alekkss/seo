"""
Проверка: битые внутренние и внешние ссылки.

Как работает:
  1. Для каждой проверяемой страницы извлекает все <a href>.
  2. Разделяет на внутренние и внешние.
  3. Для каждой уникальной ссылки делает async HEAD-запрос.
     Если HEAD возвращает 405 — повторяет GET (автоматически в async_head).
  4. Считает битой ссылку с кодом 4xx/5xx или ошибкой соединения.
  5. Возвращает отчёт «источник → битая ссылка → причина».

Два режима вызова:
  - check(...) — синхронная обёртка (вызывается из audit_service через executor)
  - async_check(...) — асинхронная функция (для прямого вызова из async-кода)
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..utils import (
    AsyncResponse,
    async_head,
    create_aiohttp_session,
    is_html_response,
    normalize_url,
    parse_html,
)
from ..crawler import extract_links

CHECK_NAME = "broken_links"
DESCRIPTION = "Битые внутренние и внешние ссылки"

# Коды, которые считаем «нормальными» (не битыми)
_OK_CODES: frozenset[int] = frozenset(range(200, 400))

# Прогресс выводится каждые N проверенных URL
_PROGRESS_EVERY: int = 100


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная проверка одного URL
# ═════════════════════════════════════════════════════════════════════════════

async def _async_probe_url(
    url: str,
    *,
    session: Any,
    semaphore: asyncio.Semaphore,
    timeout: int = 12,
) -> dict[str, Any]:
    """
    Асинхронный HEAD-запрос к URL для проверки доступности.

    Если сервер не поддерживает HEAD (405/501), async_head
    автоматически выполнит GET-запрос.

    Args:
        url: проверяемый URL.
        session: aiohttp-сессия.
        semaphore: ограничитель параллельности.
        timeout: таймаут запроса в секундах.

    Returns:
        Словарь {"url", "status_code", "ok", "error"}.
    """
    result: dict[str, Any] = {
        "url": url,
        "status_code": None,
        "ok": False,
        "error": "",
    }

    resp = await async_head(
        url,
        session=session,
        semaphore=semaphore,
        timeout=timeout,
        retries=1,
        retry_delay=1.0,
    )

    if resp.error and resp.status == 0:
        result["error"] = resp.error
        return result

    result["status_code"] = resp.status
    result["ok"] = resp.status in _OK_CODES
    if not result["ok"]:
        result["error"] = f"HTTP {resp.status}"

    return result


# ═════════════════════════════════════════════════════════════════════════════
# Сбор ссылок со страницы
# ═════════════════════════════════════════════════════════════════════════════

def _collect_links_from_page(page: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает ссылки с одной страницы.

    Работает и с requests.Response, и с AsyncResponse.

    Args:
        page: словарь {"url", "resp", "html"}.

    Returns:
        Словарь {"url": ..., "internal": [...], "external": [...]}.
    """
    url = page["url"]
    html = page.get("html")

    if html is None:
        resp = page.get("resp")
        if resp is None or isinstance(resp, Exception):
            return {"url": url, "internal": [], "external": []}

        # Проверяем тип ответа (AsyncResponse или requests.Response)
        if isinstance(resp, AsyncResponse):
            if not resp.ok or resp.status != 200:
                return {"url": url, "internal": [], "external": []}
            if not is_html_response(resp):
                return {"url": url, "internal": [], "external": []}
            html = resp.text
        else:
            if not is_html_response(resp):
                return {"url": url, "internal": [], "external": []}
            html = resp.text

    links = extract_links(html, url)
    return {
        "url": url,
        "internal": links["internal"],
        "external": links["external"],
    }


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная пакетная проверка
# ═════════════════════════════════════════════════════════════════════════════

async def async_check(
    pages: list[dict[str, Any]],
    *,
    check_external: bool = True,
    max_concurrent: int = 50,
    timeout: int = 12,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Асинхронная проверка всех ссылок на доступность.

    Этапы:
      1. Извлекает все ссылки со всех страниц.
      2. Дедуплицирует целевые URL.
      3. Параллельно проверяет каждый уникальный URL через aiohttp.
      4. Формирует отчёт по битым ссылкам.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        check_external: проверять ли внешние ссылки.
        max_concurrent: максимум одновременных запросов.
        timeout: таймаут HTTP-запроса в секундах.
        verbose: выводить ли прогресс в консоль.

    Returns:
        Список словарей с информацией о битых ссылках.
    """
    # ── Шаг 1: собираем все ссылки ──────────────────────────────────────
    internal_map: dict[str, set[str]] = {}
    external_map: dict[str, set[str]] = {}

    for page in pages:
        info = _collect_links_from_page(page)
        for link in info["internal"]:
            norm = normalize_url(link)
            internal_map.setdefault(norm, set()).add(page["url"])
        if check_external:
            for link in info["external"]:
                external_map.setdefault(link, set()).add(page["url"])

    all_targets: dict[str, str] = {}
    for u in internal_map:
        all_targets[u] = "internal"
    for u in external_map:
        all_targets[u] = "external"

    if verbose:
        print(
            f"  [{CHECK_NAME}] Уникальных ссылок: "
            f"{len(internal_map)} внутренних, {len(external_map)} внешних. "
            f"Проверяю..."
        )

    if not all_targets:
        return []

    # ── Шаг 2: асинхронная проверка HEAD/GET ────────────────────────────
    semaphore = asyncio.Semaphore(max_concurrent)
    session = create_aiohttp_session(
        max_concurrent=max_concurrent,
        timeout_total=timeout + 10,
        timeout_connect=min(timeout, 10),
    )

    probe_results: dict[str, dict[str, Any]] = {}
    done_count = 0
    total = len(all_targets)
    lock = asyncio.Lock()

    async def _probe_one(url: str) -> tuple[str, dict[str, Any]]:
        nonlocal done_count
        result = await _async_probe_url(
            url,
            session=session,
            semaphore=semaphore,
            timeout=timeout,
        )
        async with lock:
            done_count += 1
            if verbose and done_count % _PROGRESS_EVERY == 0:
                print(f"    ...проверено {done_count}/{total}")
        return url, result

    try:
        tasks = [_probe_one(url) for url in all_targets]
        results_list = await asyncio.gather(*tasks)
    finally:
        await session.close()

    for url, result in results_list:
        probe_results[url] = result

    # ── Шаг 3: формируем отчёт по битым ────────────────────────────────
    broken: list[dict[str, Any]] = []

    for target_url, probe in probe_results.items():
        if probe["ok"]:
            continue

        link_type = all_targets[target_url]
        sources = (
            internal_map.get(target_url, set())
            if link_type == "internal"
            else external_map.get(target_url, set())
        )

        for src in sources:
            broken.append({
                "check": CHECK_NAME,
                "source_url": src,
                "target_url": target_url,
                "link_type": link_type,
                "status_code": probe["status_code"],
                "error": probe["error"],
            })

    if verbose:
        print(f"  [{CHECK_NAME}] Битых ссылок: {len(broken)}")

    return broken


# ═════════════════════════════════════════════════════════════════════════════
# Синхронная обёртка (обратная совместимость с audit_service)
# ═════════════════════════════════════════════════════════════════════════════

def check(
    pages: list[dict[str, Any]],
    *,
    check_external: bool = True,
    workers: int = 15,
    timeout: int = 12,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Синхронная обёртка над async_check.

    Вызывается из audit_service._execute_single_check внутри executor.
    Создаёт новый event loop для выполнения асинхронной проверки.

    Параметр workers преобразуется в max_concurrent для aiohttp:
    множитель x3 даёт хорошее соотношение параллельности и нагрузки.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        check_external: проверять ли внешние ссылки.
        workers: количество воркеров (преобразуется в max_concurrent).
        timeout: таймаут HTTP-запроса в секундах.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о битых ссылках.
    """
    max_concurrent = min(workers * 3, 80)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            async_check(
                pages,
                check_external=check_external,
                max_concurrent=max_concurrent,
                timeout=timeout,
                verbose=verbose,
            )
        )
    finally:
        loop.close()


# ═════════════════════════════════════════════════════════════════════════════
# Фильтры и сводка
# ═════════════════════════════════════════════════════════════════════════════

def filter_internal(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только внутренние битые ссылки."""
    return [r for r in results if r["link_type"] == "internal"]


def filter_external(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только внешние битые ссылки."""
    return [r for r in results if r["link_type"] == "external"]


def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    internal = filter_internal(results)
    external = filter_external(results)

    lines = [
        f"[{CHECK_NAME}] Битых: {len(results)} "
        f"(внутренних {len(internal)}, внешних {len(external)})"
    ]

    if internal:
        lines.append("  Внутренние:")
        for r in internal:
            lines.append(
                f"    ✗ {r['source_url']}  →  {r['target_url']}  "
                f"({r['error']})"
            )

    if external:
        lines.append("  Внешние:")
        for r in external:
            lines.append(
                f"    ✗ {r['source_url']}  →  {r['target_url']}  "
                f"({r['error']})"
            )

    return "\n".join(lines)
