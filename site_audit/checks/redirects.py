"""
Проверка: редиректы.

Что проверяется:
  - Цепочки редиректов длиной 2+ (301→301→200)
  - Циклические редиректы (бесконечные петли)
  - Редиректы на внешний домен
  - Внутренние ссылки, ведущие на URL с цепочкой редиректов 2+
  - HTTP → HTTPS редиректы (mixed scheme)
  - Одиночные редиректы (1 хоп) НЕ считаются проблемой

Два режима вызова:
  - check(...) / check_internal_links_to_redirects(...) — синхронные обёртки
  - async_check(...) / async_check_internal_links(...) — асинхронные функции
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urljoin, urlparse

from ..utils import (
    AsyncResponse,
    async_fetch,
    create_aiohttp_session,
    get_domain,
    is_html_response,
)
from ..crawler import extract_links

CHECK_NAME = "redirects"
DESCRIPTION = "Цепочки редиректов, циклы, HTTP↔HTTPS"

# Минимальная длина цепочки, которая считается проблемой
MIN_CHAIN_TO_REPORT: int = 2

# Максимальное количество хопов при трассировке одного URL
_MAX_HOPS: int = 10

# Прогресс выводится каждые N проверенных URL
_PROGRESS_EVERY: int = 100


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная трассировка редиректов одного URL
# ═════════════════════════════════════════════════════════════════════════════

async def _async_trace_redirects(
    url: str,
    *,
    session: Any,
    semaphore: asyncio.Semaphore,
    timeout: int = 12,
) -> dict[str, Any]:
    """
    Асинхронно трассирует цепочку редиректов одного URL.

    Выполняет запросы с allow_redirects=False, вручную следуя
    за заголовком Location. Это позволяет записать каждый хоп
    в цепочке и обнаружить циклы.

    Семафор ограничивает параллельность трассировок,
    но хопы внутри одной трассировки идут последовательно.

    Args:
        url: трассируемый URL.
        session: aiohttp-сессия.
        semaphore: ограничитель параллельности.
        timeout: таймаут каждого хопа в секундах.

    Returns:
        Словарь с результатами трассировки.
    """
    result: dict[str, Any] = {
        "url": url,
        "chain": [],
        "final_url": url,
        "final_status": None,
        "is_redirect": False,
        "chain_length": 0,
        "is_loop": False,
        "error": "",
    }

    async with semaphore:
        current = url
        visited: set[str] = set()

        for _ in range(_MAX_HOPS + 1):
            if current in visited:
                result["is_loop"] = True
                result["error"] = "Циклический редирект"
                break
            visited.add(current)

            resp = await async_fetch(
                current,
                session=session,
                method="GET",
                timeout=timeout,
                retries=1,
                retry_delay=1.0,
                allow_redirects=False,
                read_body=False,
            )

            if resp.error and resp.status == 0:
                result["error"] = resp.error
                break

            result["chain"].append((current, resp.status))
            result["final_status"] = resp.status

            if 300 <= resp.status < 400:
                location = resp.header("Location", "").strip()
                if not location:
                    result["error"] = (
                        f"HTTP {resp.status} без заголовка Location"
                    )
                    break
                if not location.startswith("http"):
                    location = urljoin(current, location)
                current = location
            else:
                result["final_url"] = current
                break
        else:
            result["error"] = (
                f"Слишком длинная цепочка (>{_MAX_HOPS} хопов)"
            )

    redirects_in_chain = [
        c for c in result["chain"] if 300 <= c[1] < 400
    ]
    result["chain_length"] = len(redirects_in_chain)
    result["is_redirect"] = result["chain_length"] > 0

    return result


# ═════════════════════════════════════════════════════════════════════════════
# Анализ результатов трассировки
# ═════════════════════════════════════════════════════════════════════════════

def _analyze_trace(trace: dict[str, Any], site_domain: str) -> list[str]:
    """
    Анализирует результат трассировки и возвращает список проблем.

    Args:
        trace: результат _async_trace_redirects.
        site_domain: домен проверяемого сайта.

    Returns:
        Список строк-описаний проблем (пустой, если проблем нет).
    """
    issues: list[str] = []

    if trace["is_loop"]:
        issues.append("Циклический редирект")
        return issues

    if trace["error"] and not trace["is_redirect"]:
        issues.append(f"Ошибка: {trace['error']}")
        return issues

    if not trace["is_redirect"]:
        return issues

    if trace["chain_length"] >= MIN_CHAIN_TO_REPORT:
        issues.append(
            f"Длинная цепочка ({trace['chain_length']} хопов): "
            + " → ".join(c[0] for c in trace["chain"])
        )

    final_domain = get_domain(trace["final_url"])
    if final_domain != site_domain:
        issues.append(f"Редирект на внешний домен: {trace['final_url']}")

    orig_scheme = urlparse(trace["url"]).scheme
    final_scheme = urlparse(trace["final_url"]).scheme
    if orig_scheme == "http" and final_scheme == "https":
        issues.append(
            "HTTP → HTTPS редирект (ссылка ведёт на http-версию)"
        )
    elif orig_scheme == "https" and final_scheme == "http":
        issues.append(
            "HTTPS → HTTP редирект (даунгрейд, проблема безопасности)"
        )

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная проверка списка URL
# ═════════════════════════════════════════════════════════════════════════════

async def async_check(
    urls: list[str],
    *,
    site_domain: str | None = None,
    max_concurrent: int = 30,
    timeout: int = 12,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Асинхронно проверяет список URL на проблемы с редиректами.

    Каждый URL трассируется параллельно (ограничено семафором),
    но хопы внутри одной трассировки идут последовательно.

    Args:
        urls: список URL для проверки.
        site_domain: домен сайта (для определения внешних редиректов).
        max_concurrent: максимум одновременных трассировок.
        timeout: таймаут каждого хопа в секундах.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о проблемных редиректах.
    """
    if site_domain is None and urls:
        site_domain = get_domain(urls[0])

    if verbose:
        print(f"  [{CHECK_NAME}] Проверяю {len(urls)} URL на редиректы...")

    if not urls:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    session = create_aiohttp_session(
        max_concurrent=max_concurrent,
        timeout_total=timeout + 10,
        timeout_connect=min(timeout, 8),
    )

    done_count = 0
    total = len(urls)
    lock = asyncio.Lock()
    traces: dict[str, dict[str, Any]] = {}

    async def _trace_one(url: str) -> tuple[str, dict[str, Any]]:
        nonlocal done_count
        trace = await _async_trace_redirects(
            url,
            session=session,
            semaphore=semaphore,
            timeout=timeout,
        )
        async with lock:
            done_count += 1
            if verbose and done_count % _PROGRESS_EVERY == 0:
                print(f"    ...проверено {done_count}/{total}")
        return url, trace

    try:
        tasks = [_trace_one(url) for url in urls]
        results_list = await asyncio.gather(*tasks)
    finally:
        await session.close()

    for url, trace in results_list:
        traces[url] = trace

    # Анализируем результаты
    results: list[dict[str, Any]] = []
    for url in urls:
        trace = traces[url]
        issues = _analyze_trace(trace, site_domain or "")
        if not issues:
            continue
        results.append({
            "check": CHECK_NAME,
            "url": url,
            "final_url": trace["final_url"],
            "chain_length": trace["chain_length"],
            "chain": trace["chain"],
            "issues": issues,
        })

    if verbose:
        print(f"  [{CHECK_NAME}] Проблемных: {len(results)}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная проверка внутренних ссылок на цепочки редиректов
# ═════════════════════════════════════════════════════════════════════════════

async def async_check_internal_links(
    pages: list[dict[str, Any]],
    *,
    max_concurrent: int = 30,
    timeout: int = 12,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Асинхронно проверяет внутренние ссылки на наличие цепочек редиректов.

    Извлекает все внутренние ссылки со страниц, дедуплицирует их,
    трассирует каждую уникальную ссылку и находит те, что ведут
    на цепочку из 2+ хопов.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        max_concurrent: максимум одновременных трассировок.
        timeout: таймаут каждого хопа.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о ссылках на цепочки.
    """
    # Собираем карту: target_url → set(source_urls)
    link_map: dict[str, set[str]] = {}

    for page in pages:
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

        links = extract_links(html, page["url"])
        for link in links["internal"]:
            link_map.setdefault(link, set()).add(page["url"])

    unique_targets = list(link_map.keys())

    if verbose:
        print(
            f"  [{CHECK_NAME}] Проверяю {len(unique_targets)} "
            f"внутренних ссылок на цепочки редиректов..."
        )

    if not unique_targets:
        return []

    # Трассируем все уникальные ссылки
    semaphore = asyncio.Semaphore(max_concurrent)
    session = create_aiohttp_session(
        max_concurrent=max_concurrent,
        timeout_total=timeout + 10,
        timeout_connect=min(timeout, 8),
    )

    done_count = 0
    total = len(unique_targets)
    lock = asyncio.Lock()
    traces: dict[str, dict[str, Any]] = {}

    async def _trace_one(url: str) -> tuple[str, dict[str, Any]]:
        nonlocal done_count
        trace = await _async_trace_redirects(
            url,
            session=session,
            semaphore=semaphore,
            timeout=timeout,
        )
        async with lock:
            done_count += 1
            if verbose and done_count % _PROGRESS_EVERY == 0:
                print(f"    ...проверено {done_count}/{total}")
        return url, trace

    try:
        tasks = [_trace_one(url) for url in unique_targets]
        results_list = await asyncio.gather(*tasks)
    finally:
        await session.close()

    for url, trace in results_list:
        traces[url] = trace

    # Формируем отчёт
    results: list[dict[str, Any]] = []
    for target, trace in traces.items():
        if not trace["is_redirect"]:
            continue
        if trace["is_loop"]:
            continue
        if trace["chain_length"] < MIN_CHAIN_TO_REPORT:
            continue

        for source in link_map[target]:
            results.append({
                "check": CHECK_NAME,
                "type": "link_to_redirect",
                "source_url": source,
                "linked_url": target,
                "final_url": trace["final_url"],
                "chain_length": trace["chain_length"],
                "issues": [
                    f"Ссылка ведёт на цепочку редиректов "
                    f"({trace['chain_length']} хопов) → {trace['final_url']}"
                ],
            })

    if verbose:
        print(
            f"  [{CHECK_NAME}] Ссылок на цепочки редиректов: {len(results)}"
        )

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Синхронные обёртки (обратная совместимость с audit_service)
# ═════════════════════════════════════════════════════════════════════════════

def check(
    urls: list[str],
    *,
    site_domain: str | None = None,
    workers: int = 10,
    timeout: int = 12,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Синхронная обёртка над async_check.

    Args:
        urls: список URL для проверки.
        site_domain: домен сайта.
        workers: количество воркеров (преобразуется в max_concurrent).
        timeout: таймаут запроса.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о проблемных редиректах.
    """
    max_concurrent = min(workers * 3, 60)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            async_check(
                urls,
                site_domain=site_domain,
                max_concurrent=max_concurrent,
                timeout=timeout,
                verbose=verbose,
            )
        )
    finally:
        loop.close()


def check_internal_links_to_redirects(
    pages: list[dict[str, Any]],
    *,
    workers: int = 10,
    timeout: int = 12,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Синхронная обёртка над async_check_internal_links.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        workers: количество воркеров (преобразуется в max_concurrent).
        timeout: таймаут запроса.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о ссылках на цепочки.
    """
    max_concurrent = min(workers * 3, 60)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            async_check_internal_links(
                pages,
                max_concurrent=max_concurrent,
                timeout=timeout,
                verbose=verbose,
            )
        )
    finally:
        loop.close()


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    loops = [
        r for r in results
        if any("Циклич" in i for i in r.get("issues", []))
    ]
    chains = [
        r for r in results
        if any("Длинная цепочка" in i for i in r.get("issues", []))
    ]
    external = [
        r for r in results
        if any("внешний домен" in i for i in r.get("issues", []))
    ]
    scheme = [
        r for r in results
        if any(
            "HTTP" in i and "редирект" in i
            for i in r.get("issues", [])
        )
    ]
    link_redir = [
        r for r in results if r.get("type") == "link_to_redirect"
    ]

    lines = [
        f"[{CHECK_NAME}] Проблем: {len(results)} "
        f"(циклы: {len(loops)}, длинные цепочки: {len(chains)}, "
        f"на внешние: {len(external)}, HTTP↔HTTPS: {len(scheme)}, "
        f"ссылки на цепочки: {len(link_redir)})"
    ]

    for r in results:
        issues_str = "; ".join(r.get("issues", []))
        url = r.get("url") or r.get("linked_url", "?")
        lines.append(f"  ✗ {url}  —  {issues_str}")

    return "\n".join(lines)
