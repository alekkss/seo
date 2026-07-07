# site_audit/checks/images.py
"""
Проверка: изображения.

Что проверяется:
  - Битые картинки (HEAD-запрос → не 200)
  - Тяжёлые картинки (Content-Length выше порога)
  - Устаревший формат (BMP, TIFF) вместо WebP/AVIF
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from ..utils import fetch, head, is_html_response
from ..crawler import extract_images

CHECK_NAME = "images"
DESCRIPTION = "Битые и тяжёлые картинки"

DEFAULT_MAX_SIZE_KB = 500
_OUTDATED_FORMATS = frozenset((".bmp", ".tiff", ".tif"))


# ── Проверка одного изображения по URL ──────────────────────────────────────

def _probe_image(src: str, *, timeout: int = 10, max_size_kb: int = DEFAULT_MAX_SIZE_KB) -> dict:
    result = {
        "src": src,
        "status_code": None,
        "ok": False,
        "content_length": None,
        "content_type": "",
        "is_heavy": False,
        "is_outdated_format": False,
        "error": "",
    }

    resp = head(src, timeout=timeout, retries=1, retry_delay=1.0)

    if isinstance(resp, requests.Response) and resp.status_code in (405, 501):
        resp = fetch(src, timeout=timeout, retries=1, retry_delay=1.0)

    if isinstance(resp, Exception):
        result["error"] = str(resp)
        return result

    result["status_code"] = resp.status_code
    result["content_type"] = resp.headers.get("Content-Type", "")

    if resp.status_code != 200:
        result["error"] = f"HTTP {resp.status_code}"
        return result

    result["ok"] = True

    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        size = int(cl)
        result["content_length"] = size
        if size > max_size_kb * 1024:
            result["is_heavy"] = True

    path = urlparse(src).path.lower()
    ext = ""
    if "." in path:
        ext = "." + path.rsplit(".", 1)[-1]
    if ext in _OUTDATED_FORMATS:
        result["is_outdated_format"] = True

    return result


# ── Сбор изображений со страницы ────────────────────────────────────────────

def collect_from_page(url: str, *,
                      resp: requests.Response | None = None,
                      html: str | None = None) -> list[dict]:
    if html is None:
        if resp is None:
            resp = fetch(url)
        if isinstance(resp, Exception):
            return []
        if not is_html_response(resp):
            return []
        html = resp.text

    images = extract_images(html, url)
    for img in images:
        img["page_url"] = url
    return images


# ── Пакетная проверка ───────────────────────────────────────────────────────

def check(pages: list[dict], *,
          max_size_kb: int = DEFAULT_MAX_SIZE_KB,
          workers: int = 15,
          timeout: int = 10,
          verbose: bool = True) -> list[dict]:
    # src → [{"page_url": ...}, ...]
    image_map: dict[str, list[dict]] = {}

    for page in pages:
        imgs = collect_from_page(
            page["url"],
            resp=page.get("resp"),
            html=page.get("html"),
        )
        for img in imgs:
            image_map.setdefault(img["src"], []).append({
                "page_url": img["page_url"],
            })

    unique_srcs = list(image_map.keys())
    if verbose:
        print(f"  [{CHECK_NAME}] Найдено {len(unique_srcs)} уникальных изображений.")

    probes: dict[str, dict] = {}
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_probe_image, src, timeout=timeout, max_size_kb=max_size_kb): src
            for src in unique_srcs
        }
        for future in as_completed(futures):
            src = futures[future]
            probes[src] = future.result()
            done += 1
            if verbose and done % 100 == 0:
                print(f"    ...проверено {done}/{len(unique_srcs)}")

    results: list[dict] = []

    for src, occurrences in image_map.items():
        probe = probes[src]

        for occ in occurrences:
            issues: list[str] = []

            if not probe["ok"]:
                issues.append(f"Битая ({probe['error']})")

            if probe["is_heavy"] and probe["content_length"]:
                size_kb = probe["content_length"] / 1024
                issues.append(f"Тяжёлая ({size_kb:.0f} КБ, порог {max_size_kb} КБ)")

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


# ── Фильтры ─────────────────────────────────────────────────────────────────

def filter_broken(results: list[dict]) -> list[dict]:
    return [r for r in results if any("Битая" in i for i in r["issues"])]


def filter_heavy(results: list[dict]) -> list[dict]:
    return [r for r in results if any("Тяжёлая" in i for i in r["issues"])]


# ── Сводка ──────────────────────────────────────────────────────────────────

def summary(results: list[dict]) -> str:
    broken = filter_broken(results)
    heavy = filter_heavy(results)

    lines = [
        f"[{CHECK_NAME}] Проблем: {len(results)} "
        f"(битых {len(broken)}, тяжёлых {len(heavy)})"
    ]

    if broken:
        lines.append("  Битые:")
        seen = set()
        for r in broken:
            if r["src"] not in seen:
                seen.add(r["src"])
                lines.append(f"    ✗ {r['src']}  (на {r['page_url']})")

    if heavy:
        lines.append("  Тяжёлые:")
        seen = set()
        for r in heavy:
            if r["src"] not in seen:
                seen.add(r["src"])
                size_str = ""
                if r["content_length"]:
                    size_str = f" ({r['content_length'] / 1024:.0f} КБ)"
                lines.append(f"    ⚠ {r['src']}{size_str}")

    return "\n".join(lines)
