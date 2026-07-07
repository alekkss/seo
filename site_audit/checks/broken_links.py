# site_audit/checks/broken_links.py
"""
Проверка: битые внутренние и внешние ссылки.

Как работает:
  1. Для каждой проверяемой страницы извлекает все <a href>.
  2. Разделяет на внутренние и внешние.
  3. Для каждой уникальной ссылки делает HEAD-запрос (быстрее GET).
     Если HEAD возвращает 405 — повторяет GET.
  4. Считает битой ссылку с кодом 4xx/5xx или ошибкой соединения.
  5. Возвращает отчёт «источник → битая ссылка → причина».
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ..utils import fetch, head, is_html_response, normalize_url
from ..crawler import extract_links

CHECK_NAME = "broken_links"
DESCRIPTION = "Битые внутренние и внешние ссылки"

# Коды, которые считаем «нормальными» (не битыми)
_OK_CODES = frozenset(range(200, 400))


# ── Проверка одного URL ─────────────────────────────────────────────────────

def _probe_url(url: str, *, timeout: int = 12) -> dict:
    """
    HEAD-запрос к URL. Если сервер не любит HEAD (405/501) — пробуем GET.
    Возвращает {"url", "status_code", "ok", "error"}.
    """
    result = {
        "url": url,
        "status_code": None,
        "ok": False,
        "error": "",
    }
    resp = head(url, timeout=timeout, retries=1, retry_delay=1.0)

    # HEAD не поддерживается — пробуем GET
    if isinstance(resp, requests.Response) and resp.status_code in (405, 501):
        resp = fetch(url, timeout=timeout, retries=1, retry_delay=1.0)

    if isinstance(resp, Exception):
        result["error"] = str(resp)
        return result

    result["status_code"] = resp.status_code
    result["ok"] = resp.status_code in _OK_CODES
    if not result["ok"]:
        result["error"] = f"HTTP {resp.status_code}"

    return result


# ── Сбор ссылок с одной страницы ────────────────────────────────────────────

def collect_from_page(url: str, *,
                      resp: requests.Response | None = None,
                      html: str | None = None) -> dict:
    """
    Извлекает ссылки со страницы.
    Возвращает {"url": ..., "internal": [...], "external": [...]}.
    """
    if html is None:
        if resp is None:
            resp = fetch(url)
        if isinstance(resp, Exception):
            return {"url": url, "internal": [], "external": []}
        if not is_html_response(resp):
            return {"url": url, "internal": [], "external": []}
        html = resp.text

    links = extract_links(html, url)
    return {
        "url": url,
        "internal": links["internal"],
        "external": links["external"],
    }


# ── Пакетная проверка ───────────────────────────────────────────────────────

def check(pages: list[dict], *,
          check_external: bool = True,
          workers: int = 15,
          timeout: int = 12,
          verbose: bool = True) -> list[dict]:
    """
    Полный цикл:
      1. Собирает ссылки со всех страниц.
      2. Дедуплицирует целевые URL.
      3. Проверяет каждый уникальный URL.
      4. Возвращает список битых записей.

    pages — список словарей {"url": ..., "resp"?: ..., "html"?: ...}.

    Возвращает список словарей:
        {
            "check": "broken_links",
            "source_url":  откуда ведёт ссылка,
            "target_url":  куда ведёт ссылка,
            "link_type":   "internal" | "external",
            "status_code": int | None,
            "error":       str,
        }
    """
    # ── Шаг 1: собираем все ссылки ──────────────────────────────────────
    # target_url → set(source_urls)
    internal_map: dict[str, set[str]] = {}
    external_map: dict[str, set[str]] = {}

    for page in pages:
        info = collect_from_page(
            page["url"],
            resp=page.get("resp"),
            html=page.get("html"),
        )
        for link in info["internal"]:
            norm = normalize_url(link)
            internal_map.setdefault(norm, set()).add(page["url"])
        if check_external:
            for link in info["external"]:
                external_map.setdefault(link, set()).add(page["url"])

    all_targets: dict[str, str] = {}  # url → "internal" | "external"
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

    # ── Шаг 2: проверяем HEAD/GET ──────────────────────────────────────
    probe_results: dict[str, dict] = {}
    done = 0
    total = len(all_targets)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_probe_url, url, timeout=timeout): url
            for url in all_targets
        }
        for future in as_completed(futures):
            url = futures[future]
            probe_results[url] = future.result()
            done += 1
            if verbose and done % 50 == 0:
                print(f"    ...проверено {done}/{total}")

    # ── Шаг 3: формируем отчёт по битым ────────────────────────────────
    broken: list[dict] = []

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


# ── Фильтры и сводка ────────────────────────────────────────────────────────

def filter_internal(results: list[dict]) -> list[dict]:
    return [r for r in results if r["link_type"] == "internal"]


def filter_external(results: list[dict]) -> list[dict]:
    return [r for r in results if r["link_type"] == "external"]


def summary(results: list[dict]) -> str:
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
