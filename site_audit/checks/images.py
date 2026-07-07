"""
Проверка: изображения.

Что проверяется:
  - Битые картинки (HEAD-запрос → не 200)
  - Тяжёлые картинки (Content-Length выше порога)
  - Устаревший формат (BMP, TIFF) вместо WebP/AVIF

Два режима вызова:
  - check(...) — синхронная обёртка (вызывается из audit_service через executor)
  - async_check(...) — асинхронная функция (для прямого вызова из async-кода)
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from ..utils import (
    AsyncResponse,
    async_head,
    create_aiohttp_session,
    is_html_response,
)
from ..crawler import extract_images

CHECK_NAME = "images"
DESCRIPTION = "Битые и тяжёлые картинки"

DEFAULT_MAX_SIZE_KB: int = 500

_OUTDATED_FORMATS: frozenset[str] = frozenset((".bmp", ".tiff", ".tif"))

# Прогресс выводится каждые N проверенных изображений
_PROGRESS_EVERY: int = 200


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная проверка одного изображения
# ═════════════════════════════════════════════════════════════════════════════

async def _async_probe_image(
    src: str,
    *,
    session: Any,
    semaphore: asyncio.Semaphore,
    timeout: int = 10,
    max_size_kb: int = DEFAULT_MAX_SIZE_KB,
) -> dict[str, Any]:
    """
    Асинхронный HEAD-запрос к изображению для проверки доступности и размера.

    Если сервер не поддерживает HEAD (405/501), async_head
    автоматически выполнит GET-запрос.

    Args:
        src: URL изображения.
        session: aiohttp-сессия.
        semaphore: ограничитель параллельности.
        timeout: таймаут запроса в секундах.
        max_size_kb: порог тяжёлой картинки в килобайтах.

    Returns:
        Словарь с результатами проверки.
    """
    result: dict[str, Any] = {
        "src": src,
        "status_code": None,
        "ok": False,
        "content_length": None,
        "content_type": "",
        "is_heavy": False,
        "is_outdated_format": False,
        "error": "",
    }

    resp = await async_head(
        src,
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
    result["content_type"] = resp.header("Content-Type", "")

    if resp.status != 200:
        result["error"] = f"HTTP {resp.status}"
        return result

    result["ok"] = True

    # Проверяем размер
    cl = resp.header("Content-Length", "")
    if cl and cl.isdigit():
        size = int(cl)
        result["content_length"] = size
        if size > max_size_kb * 1024:
            result["is_heavy"] = True

    # Проверяем формат по расширению URL
    path = urlparse(src).path.lower()
    ext = ""
    if "." in path:
        ext = "." + path.rsplit(".", 1)[-1]
    if ext in _OUTDATED_FORMATS:
        result["is_outdated_format"] = True

    return result


# ═════════════════════════════════════════════════════════════════════════════
# Сбор изображений со страницы
# ═════════════════════════════════════════════════════════════════════════════

def _collect_images_from_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Извлекает изображения с одной страницы.

    Работает и с requests.Response, и с AsyncResponse.

    Args:
        page: словарь {"url", "resp", "html"}.

    Returns:
        Список словарей {"src", "alt", "has_alt", "page_url"}.
    """
    url = page["url"]
    html = page.get("html")

    if html is None:
        resp = page.get("resp")
        if resp is None or isinstance(resp, Exception):
            return []

        if isinstance(resp, AsyncResponse):
            if not resp.ok or resp.status != 200:
                return []
            if not is_html_response(resp):
                return []
            html = resp.text
        else:
            if not is_html_response(resp):
                return []
            html = resp.text

    images = extract_images(html, url)
    for img in images:
        img["page_url"] = url
    return images


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная пакетная проверка
# ═════════════════════════════════════════════════════════════════════════════

async def async_check(
    pages: list[dict[str, Any]],
    *,
    max_size_kb: int = DEFAULT_MAX_SIZE_KB,
    max_concurrent: int = 60,
    timeout: int = 10,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Асинхронная проверка всех изображений на доступность и размер.

    Этапы:
      1. Извлекает все изображения со всех страниц.
      2. Дедуплицирует по URL (src).
      3. Параллельно проверяет каждое уникальное изображение через aiohttp HEAD.
      4. Формирует отчёт по проблемным изображениям.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        max_size_kb: порог тяжёлой картинки в килобайтах.
        max_concurrent: максимум одновременных запросов.
        timeout: таймаут HTTP-запроса в секундах.
        verbose: выводить ли прогресс в консоль.

    Returns:
        Список словарей с информацией о проблемных изображениях.
    """
    # ── Шаг 1: собираем все изображения ─────────────────────────────────
    # src → [{"page_url": ...}, ...]
    image_map: dict[str, list[dict[str, Any]]] = {}

    for page in pages:
        imgs = _collect_images_from_page(page)
        for img in imgs:
            image_map.setdefault(img["src"], []).append({
                "page_url": img["page_url"],
            })

    unique_srcs = list(image_map.keys())

    if verbose:
        print(f"  [{CHECK_NAME}] Найдено {len(unique_srcs)} уникальных изображений.")

    if not unique_srcs:
        return []

    # ── Шаг 2: асинхронная проверка HEAD ────────────────────────────────
    semaphore = asyncio.Semaphore(max_concurrent)
    session = create_aiohttp_session(
        max_concurrent=max_concurrent,
        timeout_total=timeout + 10,
        timeout_connect=min(timeout, 8),
    )

    probes: dict[str, dict[str, Any]] = {}
    done_count = 0
    total = len(unique_srcs)
    lock = asyncio.Lock()

    async def _probe_one(src: str) -> tuple[str, dict[str, Any]]:
        nonlocal done_count
        result = await _async_probe_image(
            src,
            session=session,
            semaphore=semaphore,
            timeout=timeout,
            max_size_kb=max_size_kb,
        )
        async with lock:
            done_count += 1
            if verbose and done_count % _PROGRESS_EVERY == 0:
                print(f"    ...проверено {done_count}/{total}")
        return src, result

    try:
        tasks = [_probe_one(src) for src in unique_srcs]
        results_list = await asyncio.gather(*tasks)
    finally:
        await session.close()

    for src, result in results_list:
        probes[src] = result

    # ── Шаг 3: формируем отчёт ─────────────────────────────────────────
    results: list[dict[str, Any]] = []

    for src, occurrences in image_map.items():
        probe = probes[src]

        for occ in occurrences:
            issues: list[str] = []

            if not probe["ok"]:
                issues.append(f"Битая ({probe['error']})")

            if probe["is_heavy"] and probe["content_length"]:
                size_kb = probe["content_length"] / 1024
                issues.append(
                    f"Тяжёлая ({size_kb:.0f} КБ, порог {max_size_kb} КБ)"
                )

            if probe["is_outdated_format"]:
                issues.append("Устаревший формат (BMP/TIFF)")

            if not issues:
                continue

            results.append({
                "check": CHECK_NAME,
                "page_url": occ["page_url"],
                "src": src,
                "issues": issues,
                "status_code": probe["status_code"],
                "content_length": probe["content_length"],
            })

    if verbose:
        print(f"  [{CHECK_NAME}] Проблемных записей: {len(results)}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Синхронная обёртка (обратная совместимость с audit_service)
# ═════════════════════════════════════════════════════════════════════════════

def check(
    pages: list[dict[str, Any]],
    *,
    max_size_kb: int = DEFAULT_MAX_SIZE_KB,
    workers: int = 15,
    timeout: int = 10,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Синхронная обёртка над async_check.

    Вызывается из audit_service._execute_single_check внутри executor.
    Создаёт новый event loop для выполнения асинхронной проверки.

    Args:
        pages: список словарей {"url", "resp", "html"}.
        max_size_kb: порог тяжёлой картинки в килобайтах.
        workers: количество воркеров (преобразуется в max_concurrent).
        timeout: таймаут HTTP-запроса в секундах.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с информацией о проблемных изображениях.
    """
    max_concurrent = min(workers * 4, 80)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            async_check(
                pages,
                max_size_kb=max_size_kb,
                max_concurrent=max_concurrent,
                timeout=timeout,
                verbose=verbose,
            )
        )
    finally:
        loop.close()


# ═════════════════════════════════════════════════════════════════════════════
# Фильтры
# ═════════════════════════════════════════════════════════════════════════════

def filter_broken(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только битые изображения."""
    return [r for r in results if any("Битая" in i for i in r["issues"])]


def filter_heavy(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только тяжёлые изображения."""
    return [r for r in results if any("Тяжёлая" in i for i in r["issues"])]


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    broken = filter_broken(results)
    heavy = filter_heavy(results)

    lines = [
        f"[{CHECK_NAME}] Проблем: {len(results)} "
        f"(битых {len(broken)}, тяжёлых {len(heavy)})"
    ]

    if broken:
        lines.append("  Битые:")
        seen: set[str] = set()
        for r in broken:
            if r["src"] not in seen:
                seen.add(r["src"])
                lines.append(f"    ✗ {r['src']}  (на {r['page_url']})")

    if heavy:
        lines.append("  Тяжёлые:")
        seen_heavy: set[str] = set()
        for r in heavy:
            if r["src"] not in seen_heavy:
                seen_heavy.add(r["src"])
                size_str = ""
                if r["content_length"]:
                    size_str = f" ({r['content_length'] / 1024:.0f} КБ)"
                lines.append(f"    ⚠ {r['src']}{size_str}")

    return "\n".join(lines)
