# site_audit/checks/duplicates.py
"""
Проверка: дубликаты контента и мета-данных.

Что проверяется:
  - Дубликаты видимого текста (полные и near-duplicate по хешу)
  - Одинаковые <title> на разных URL
  - Одинаковые <meta description> на разных URL
  - Одинаковые H1 на разных URL
  - Страницы с одинаковым canonical (возможные непреднамеренные дубли)
  - Параметрические клоны (/page?id=1 и /page?id=2 с тем же контентом)
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import requests

from ..utils import (
    fetch, parse_html, visible_text, text_hash,
    is_html_response, normalize_url,
)

CHECK_NAME = "duplicates"
DESCRIPTION = "Дубликаты контента, title, description, H1"

# Если первые N символов видимого текста совпадают — near-duplicate
NEAR_DUP_PREFIX_LENGTH = 500

# Минимальная длина текста, чтобы вообще учитывать страницу
# (слишком короткие страницы уже ловит empty_pages)
MIN_TEXT_FOR_DUP_CHECK = 50


# ── Извлечение мета-данных со страницы ──────────────────────────────────────

def _extract_page_info(url: str, *,
                       resp: requests.Response | None = None,
                       html: str | None = None) -> dict | None:
    """
    Возвращает словарь с основными данными страницы
    или None, если страница не HTML / не 200.
    """
    if html is None:
        if resp is None:
            resp = fetch(url)
        if isinstance(resp, Exception):
            return None
        if resp.status_code != 200 or not is_html_response(resp):
            return None
        html = resp.text

    soup = parse_html(html)

    # title
    title_tag = soup.title
    title = title_tag.string.strip() if (title_tag and title_tag.string) else ""

    # description
    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = ""
    if desc_tag and desc_tag.get("content"):
        description = desc_tag["content"].strip()

    # h1
    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(strip=True) if h1_tag else ""

    # canonical
    canon_tag = soup.find("link", rel="canonical")
    canonical = ""
    if canon_tag and canon_tag.get("href"):
        canonical = canon_tag["href"].strip()

    # видимый текст
    vtext = visible_text(soup)

    return {
        "url": url,
        "title": title,
        "description": description,
        "h1": h1,
        "canonical": canonical,
        "text": vtext,
        "text_hash": text_hash(vtext) if len(vtext) >= MIN_TEXT_FOR_DUP_CHECK else "",
        "text_prefix_hash": (
            text_hash(vtext[:NEAR_DUP_PREFIX_LENGTH])
            if len(vtext) >= MIN_TEXT_FOR_DUP_CHECK
            else ""
        ),
    }


# ── Поиск дублей по произвольному полю ─────────────────────────────────────

def _find_duplicates(infos: list[dict], field: str, *,
                     min_length: int = 1) -> dict[str, list[str]]:
    """
    Группирует страницы по значению поля field.
    Возвращает {значение: [url1, url2, ...]} только для групп ≥ 2.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for info in infos:
        value = info.get(field, "")
        if len(value) >= min_length:
            groups[value].append(info["url"])
    return {k: v for k, v in groups.items() if len(v) > 1}


# ── Основная функция ────────────────────────────────────────────────────────

def check(pages: list[dict], *, verbose: bool = True) -> list[dict]:
    """
    Полный цикл проверки дубликатов.

    pages — [{"url": ..., "resp"?: ..., "html"?: ...}, ...].

    Возвращает список записей:
        {
            "check":      "duplicates",
            "dup_type":   "title" | "description" | "h1" | "content" |
                          "near_content" | "canonical",
            "value":      общее значение (или хеш),
            "urls":       [url1, url2, ...],
            "count":      количество дублей,
        }
    """
    # ── Шаг 1: извлекаем данные ────────────────────────────────────────
    if verbose:
        print(f"  [{CHECK_NAME}] Извлекаю мета-данные с {len(pages)} страниц...")

    infos: list[dict] = []
    for page in pages:
        info = _extract_page_info(
            page["url"],
            resp=page.get("resp"),
            html=page.get("html"),
        )
        if info:
            infos.append(info)

    if verbose:
        print(f"  [{CHECK_NAME}] Успешно обработано: {len(infos)} страниц.")

    results: list[dict] = []

    # ── Шаг 2: дубли title ─────────────────────────────────────────────
    dup_title = _find_duplicates(infos, "title", min_length=3)
    for value, urls in dup_title.items():
        results.append({
            "check": CHECK_NAME,
            "dup_type": "title",
            "value": value,
            "urls": urls,
            "count": len(urls),
        })

    # ── Шаг 3: дубли description ───────────────────────────────────────
    dup_desc = _find_duplicates(infos, "description", min_length=10)
    for value, urls in dup_desc.items():
        results.append({
            "check": CHECK_NAME,
            "dup_type": "description",
            "value": value,
            "urls": urls,
            "count": len(urls),
        })

    # ── Шаг 4: дубли H1 ───────────────────────────────────────────────
    dup_h1 = _find_duplicates(infos, "h1", min_length=3)
    for value, urls in dup_h1.items():
        results.append({
            "check": CHECK_NAME,
            "dup_type": "h1",
            "value": value,
            "urls": urls,
            "count": len(urls),
        })

    # ── Шаг 5: полные дубли контента (по хешу всего текста) ────────────
    dup_content = _find_duplicates(infos, "text_hash", min_length=32)
    for hash_val, urls in dup_content.items():
        # находим текст-образец для удобства
        sample = ""
        for info in infos:
            if info["text_hash"] == hash_val:
                sample = info["text"][:200]
                break
        results.append({
            "check": CHECK_NAME,
            "dup_type": "content",
            "value": f"[hash: {hash_val}] {sample}...",
            "urls": urls,
            "count": len(urls),
        })

    # ── Шаг 6: near-дубли (совпадает начало текста) ────────────────────
    dup_near = _find_duplicates(infos, "text_prefix_hash", min_length=32)
    for hash_val, urls in dup_near.items():
        # не дублируем с полными дублями
        if hash_val in dup_content:
            continue
        # исключаем группы, которые совпали как полные дубли
        full_hashes = set()
        for info in infos:
            if info["text_prefix_hash"] == hash_val:
                full_hashes.add(info["text_hash"])
        if len(full_hashes) == 1 and full_hashes.pop() in dup_content:
            continue

        sample = ""
        for info in infos:
            if info["text_prefix_hash"] == hash_val:
                sample = info["text"][:200]
                break
        results.append({
            "check": CHECK_NAME,
            "dup_type": "near_content",
            "value": f"[prefix hash: {hash_val}] {sample}...",
            "urls": urls,
            "count": len(urls),
        })

    # ── Шаг 7: одинаковый canonical у разных URL ──────────────────────
    canon_groups: dict[str, list[str]] = defaultdict(list)
    for info in infos:
        canon = info["canonical"]
        url = info["url"]
        if canon and normalize_url(canon) != normalize_url(url):
            canon_groups[normalize_url(canon)].append(url)
    for canon_val, urls in canon_groups.items():
        if len(urls) >= 2:
            results.append({
                "check": CHECK_NAME,
                "dup_type": "canonical",
                "value": canon_val,
                "urls": urls,
                "count": len(urls),
            })

    if verbose:
        print(f"  [{CHECK_NAME}] Найдено групп дублей: {len(results)}")

    return results


# ── Фильтры ─────────────────────────────────────────────────────────────────

def filter_by_type(results: list[dict], dup_type: str) -> list[dict]:
    return [r for r in results if r["dup_type"] == dup_type]


# ── Сводка ──────────────────────────────────────────────────────────────────

_TYPE_LABELS = {
    "title": "Одинаковый Title",
    "description": "Одинаковый Description",
    "h1": "Одинаковый H1",
    "content": "Полный дубль контента",
    "near_content": "Почти одинаковый контент",
    "canonical": "Общий canonical (разные URL)",
}


def summary(results: list[dict]) -> str:
    lines = [f"[{CHECK_NAME}] Найдено групп дублей: {len(results)}"]

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_type[r["dup_type"]].append(r)

    for dtype, label in _TYPE_LABELS.items():
        groups = by_type.get(dtype, [])
        if not groups:
            continue
        lines.append(f"  {label} ({len(groups)} групп):")
        for g in groups:
            val_short = g["value"][:80] + ("..." if len(g["value"]) > 80 else "")
            lines.append(f"    ✗ [{g['count']} стр.] «{val_short}»")
            for u in g["urls"]:
                lines.append(f"        {u}")

    return "\n".join(lines)
