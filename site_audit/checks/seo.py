# site_audit/checks/seo.py
"""
Проверка: базовые SEO-элементы страницы.

Что проверяется на каждой странице:
  - <title>: наличие, пустой ли, слишком короткий
  - <meta name="description">: наличие, пустой ли
  - <link rel="canonical">: наличие, совпадает ли с текущим URL
  - <meta name="robots">: наличие noindex / nofollow
  - H1: наличие, количество, пустой ли
  - Open Graph: og:title, og:description, og:image
  - JSON-LD (<script type="application/ld+json">): наличие, валидность JSON
"""

from __future__ import annotations

import json
import re

import requests

from ..utils import (
    fetch, parse_html, is_html_response, normalize_url,
)

CHECK_NAME = "seo"
DESCRIPTION = "Базовый SEO-аудит (title, description, canonical, H1, OG, JSON-LD)"

TITLE_MIN = 10


# ── Основная функция ─────────────────────────────────────────────────────────

def check(url: str, *,
          resp: requests.Response | None = None,
          html: str | None = None) -> dict:
    row = _blank_row(url)

    if resp is None and html is None:
        resp = fetch(url)

    if isinstance(resp, Exception):
        row["issues"].append("Ошибка запроса")
        return row

    if resp is not None:
        row["status_code"] = resp.status_code
        if resp.status_code != 200:
            row["issues"].append(f"HTTP {resp.status_code}")
            return row
        if not is_html_response(resp):
            row["issues"].append("Не HTML")
            return row

    source = html if html is not None else resp.text
    soup = parse_html(source)

    # ── Title ───────────────────────────────────────────────────────────
    title_tag = soup.title
    if title_tag and title_tag.string:
        title_text = title_tag.string.strip()
        row["title"] = title_text
        row["title_length"] = len(title_text)
        if len(title_text) < TITLE_MIN:
            row["issues"].append(f"Title слишком короткий ({len(title_text)} симв.)")
    else:
        row["issues"].append("Title отсутствует или пустой")

    # ── Meta description ────────────────────────────────────────────────
    desc_tag = _meta(soup, "description")
    if desc_tag is not None:
        desc_text = desc_tag.strip()
        row["meta_description"] = desc_text
        row["meta_description_length"] = len(desc_text)
        if len(desc_text) == 0:
            row["issues"].append("Meta description пустой")
    else:
        row["issues"].append("Meta description отсутствует")

    # ── Canonical ───────────────────────────────────────────────────────
    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag and canonical_tag.get("href"):
        canon = canonical_tag["href"].strip()
        row["canonical"] = canon
        if normalize_url(canon) != normalize_url(url):
            row["canonical_mismatch"] = True
            row["issues"].append(f"Canonical отличается от URL: {canon}")
    else:
        row["issues"].append("Canonical отсутствует")

    # ── Meta robots ─────────────────────────────────────────────────────
    robots = _meta(soup, "robots")
    if robots:
        row["meta_robots"] = robots
        lower = robots.lower()
        if "noindex" in lower:
            row["has_noindex"] = True
            row["issues"].append("Страница помечена noindex")

    # ── H1 ──────────────────────────────────────────────────────────────
    _check_headings(soup, row)

    # ── Open Graph ──────────────────────────────────────────────────────
    _check_og(soup, row)

    # ── JSON-LD ─────────────────────────────────────────────────────────
    _check_jsonld(soup, row)

    row["issues_count"] = len(row["issues"])
    return row


# ── Заголовки ────────────────────────────────────────────────────────────────

def _check_headings(soup, row: dict):
    h1_tags = soup.find_all("h1")
    row["h1_count"] = len(h1_tags)

    if not h1_tags:
        row["issues"].append("H1 отсутствует")
    else:
        first_h1_text = h1_tags[0].get_text(strip=True)
        row["h1_text"] = first_h1_text
        if not first_h1_text:
            row["issues"].append("H1 пустой")
        if len(h1_tags) > 1:
            row["issues"].append(f"Несколько H1 ({len(h1_tags)} шт.)")


# ── Open Graph ───────────────────────────────────────────────────────────────

_OG_REQUIRED = ("og:title", "og:description", "og:image")


def _check_og(soup, row: dict):
    og_data: dict[str, str] = {}
    for meta in soup.find_all("meta", attrs={"property": re.compile(r"^og:")}):
        prop = meta.get("property", "")
        content = meta.get("content", "").strip()
        if prop and content:
            og_data[prop] = content

    row["og"] = og_data
    missing = [tag for tag in _OG_REQUIRED if tag not in og_data]
    if missing:
        row["issues"].append(f"Отсутствуют OG-теги: {', '.join(missing)}")


# ── JSON-LD ──────────────────────────────────────────────────────────────────

def _check_jsonld(soup, row: dict):
    scripts = soup.find_all("script", type="application/ld+json")
    row["jsonld_count"] = len(scripts)

    if not scripts:
        row["issues"].append("JSON-LD (Schema.org) не найден")
        return

    for i, script in enumerate(scripts):
        raw = script.string or ""
        try:
            data = json.loads(raw)
            row.setdefault("jsonld_types", [])
            if isinstance(data, dict):
                row["jsonld_types"].append(data.get("@type", "?"))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        row["jsonld_types"].append(item.get("@type", "?"))
        except (json.JSONDecodeError, TypeError) as exc:
            row["issues"].append(f"JSON-LD #{i+1}: невалидный JSON ({exc})")


# ── Вспомогательные ─────────────────────────────────────────────────────────

def _meta(soup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": name})
    if tag and tag.get("content") is not None:
        return tag["content"]
    return None


def _blank_row(url: str) -> dict:
    return {
        "check": CHECK_NAME,
        "url": url,
        "status_code": None,
        "title": None,
        "title_length": 0,
        "meta_description": None,
        "meta_description_length": 0,
        "canonical": None,
        "canonical_mismatch": False,
        "meta_robots": None,
        "has_noindex": False,
        "has_nofollow": False,
        "h1_count": 0,
        "h1_text": None,
        "og": {},
        "jsonld_count": 0,
        "jsonld_types": [],
        "issues": [],
        "issues_count": 0,
    }


def check_many(pages: list[dict]) -> list[dict]:
    return [
        check(p["url"], resp=p.get("resp"), html=p.get("html"))
        for p in pages
    ]


def filter_with_issues(results: list[dict]) -> list[dict]:
    return [r for r in results if r.get("issues")]


def summary(results: list[dict]) -> str:
    total = len(results)
    with_issues = filter_with_issues(results)
    lines = [f"[{CHECK_NAME}] Проверено: {total}, с проблемами: {len(with_issues)}"]
    for r in with_issues:
        issues_str = "; ".join(r["issues"])
        lines.append(f"  ✗ {r['url']}  —  {issues_str}")
    return "\n".join(lines)
