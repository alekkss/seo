# site_audit/checks/placeholders.py
"""
Проверка: текстовые заглушки и незаконченный контент.

Что ищем:
  - Lorem ipsum и его вариации
  - TODO / FIXME / HACK / XXX в видимом тексте и HTML-комментариях
  - Типичные русскоязычные заглушки: «Здесь будет текст», «Заголовок страницы»,
    «Описание страницы», «Ваш текст здесь», «Пример текста» и т. д.
  - Типичные англоязычные заглушки: «Coming soon», «Under construction»,
    «Test page», «Sample text», «Insert text here» и т. д.
  - Повторяющийся мусорный текст (напр. «asdf», «test test test»)
  - HTML-комментарии с TODO/FIXME
  - Подозрительные alt у картинок: «image», «photo», «img_001»
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

from ..utils import fetch, parse_html, visible_text, is_html_response

CHECK_NAME = "placeholders"
DESCRIPTION = "Текстовые заглушки, lorem ipsum, TODO"


# ── Паттерны ────────────────────────────────────────────────────────────────

@dataclass
class _Pattern:
    regex: re.Pattern
    label: str
    severity: str = "warning"  # "warning" | "info"


_PATTERNS: list[_Pattern] = [
    # Lorem ipsum
    _Pattern(
        re.compile(r"lorem\s+ipsum", re.IGNORECASE),
        "Lorem ipsum",
    ),
    _Pattern(
        re.compile(r"dolor\s+sit\s+amet", re.IGNORECASE),
        "Lorem ipsum (dolor sit amet)",
    ),

    # TODO / FIXME в видимом тексте
    _Pattern(
        re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b"),
        "TODO/FIXME в тексте",
    ),

    # Русские заглушки
    _Pattern(
        re.compile(r"здесь\s+будет\s+текст", re.IGNORECASE),
        "Заглушка: «Здесь будет текст»",
    ),
    _Pattern(
        re.compile(r"заголовок\s+страницы", re.IGNORECASE),
        "Заглушка: «Заголовок страницы»",
    ),
    _Pattern(
        re.compile(r"описание\s+страницы", re.IGNORECASE),
        "Заглушка: «Описание страницы»",
    ),
    _Pattern(
        re.compile(r"ваш\s+текст\s+здесь", re.IGNORECASE),
        "Заглушка: «Ваш текст здесь»",
    ),
    _Pattern(
        re.compile(r"пример\s+текста", re.IGNORECASE),
        "Заглушка: «Пример текста»",
    ),
    _Pattern(
        re.compile(r"текст\s+по\s+умолчанию", re.IGNORECASE),
        "Заглушка: «Текст по умолчанию»",
    ),
    _Pattern(
        re.compile(r"введите\s+(?:текст|описание|заголовок)", re.IGNORECASE),
        "Заглушка: «Введите текст/описание»",
    ),
    _Pattern(
        re.compile(r"(?:раздел|страница)\s+в\s+разработке", re.IGNORECASE),
        "Заглушка: «Раздел в разработке»",
    ),
    _Pattern(
        re.compile(r"скоро\s+(?:здесь\s+)?(?:будет|появится|откроется)", re.IGNORECASE),
        "Заглушка: «Скоро будет/появится»",
    ),

    # Английские заглушки
    _Pattern(
        re.compile(r"\bcoming\s+soon\b", re.IGNORECASE),
        "Placeholder: Coming soon",
    ),
    _Pattern(
        re.compile(r"\bunder\s+construction\b", re.IGNORECASE),
        "Placeholder: Under construction",
    ),
    _Pattern(
        re.compile(r"\btest\s+page\b", re.IGNORECASE),
        "Placeholder: Test page",
    ),
    _Pattern(
        re.compile(r"\bsample\s+(?:text|page|content)\b", re.IGNORECASE),
        "Placeholder: Sample text/page",
    ),
    _Pattern(
        re.compile(r"\binsert\s+(?:text|content|title)\s+here\b", re.IGNORECASE),
        "Placeholder: Insert text here",
    ),
    _Pattern(
        re.compile(r"\bdefault\s+(?:title|description|text)\b", re.IGNORECASE),
        "Placeholder: Default title/text",
    ),
    _Pattern(
        re.compile(r"\buntitled\s+(?:page|document|post)\b", re.IGNORECASE),
        "Placeholder: Untitled page",
    ),
    _Pattern(
        re.compile(r"\bpage\s+title\s+here\b", re.IGNORECASE),
        "Placeholder: Page title here",
    ),

    # Мусор
    _Pattern(
        re.compile(r"\basdf{2,}", re.IGNORECASE),
        "Мусорный текст (asdf...)",
        severity="info",
    ),
    _Pattern(
        re.compile(r"\btest\s+test\s+test\b", re.IGNORECASE),
        "Мусорный текст (test test test)",
        severity="info",
    ),
]

# HTML-комментарии с TODO/FIXME
_COMMENT_PATTERN = re.compile(r"<!--(.*?)-->", re.DOTALL)
_COMMENT_TODO = re.compile(r"\b(?:TODO|FIXME|HACK|XXX|TEMP|TEMPORARY)\b", re.IGNORECASE)

# Подозрительные alt-атрибуты у картинок
_SUSPECT_ALT = re.compile(
    r"^(?:image|photo|img|picture|pic|foto|фото|картинка|изображение)"
    r"(?:\s*[\d_\-]*)?$",
    re.IGNORECASE,
)


# ── Проверка одной страницы ─────────────────────────────────────────────────

def check(url: str, *,
          resp: requests.Response | None = None,
          html: str | None = None) -> dict:
    """
    Ищет заглушки и placeholder-тексты на странице.

    Возвращает:
        {
            "check":        "placeholders",
            "url":          ...,
            "findings":     [{"label": ..., "severity": ..., "context": ..., "source": ...}, ...],
            "findings_count": int,
        }
    """
    row = {
        "check": CHECK_NAME,
        "url": url,
        "findings": [],
        "findings_count": 0,
    }

    # ── Получаем HTML ───────────────────────────────────────────────────
    if html is None:
        if resp is None:
            resp = fetch(url)
        if isinstance(resp, Exception):
            return row
        if resp.status_code != 200 or not is_html_response(resp):
            return row
        html = resp.text

    soup = parse_html(html)
    vtext = visible_text(soup)

    # ── 1. Паттерны в видимом тексте ────────────────────────────────────
    for pat in _PATTERNS:
        for match in pat.regex.finditer(vtext):
            start = max(0, match.start() - 30)
            end = min(len(vtext), match.end() + 30)
            context = vtext[start:end].replace("\n", " ").strip()
            row["findings"].append({
                "label": pat.label,
                "severity": pat.severity,
                "context": f"...{context}...",
                "source": "visible_text",
            })

    # ── 2. Паттерны в title и description ───────────────────────────────
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else ""

    for meta_field, meta_value, meta_label in [
        ("title", title, "Title"),
        ("description", description, "Meta description"),
    ]:
        for pat in _PATTERNS:
            if pat.regex.search(meta_value):
                row["findings"].append({
                    "label": f"{pat.label} в {meta_label}",
                    "severity": "warning",
                    "context": meta_value[:100],
                    "source": meta_field,
                })

    # ── 3. HTML-комментарии с TODO/FIXME ────────────────────────────────
    for comment_match in _COMMENT_PATTERN.finditer(html):
        comment_body = comment_match.group(1)
        if _COMMENT_TODO.search(comment_body):
            snippet = comment_body.strip()[:120].replace("\n", " ")
            row["findings"].append({
                "label": "TODO/FIXME в HTML-комментарии",
                "severity": "info",
                "context": f"<!-- {snippet} -->",
                "source": "html_comment",
            })

    # ── 4. Подозрительные alt у картинок ────────────────────────────────
    for img in soup.find_all("img", alt=True):
        alt_val = img["alt"].strip()
        if alt_val and _SUSPECT_ALT.match(alt_val):
            src = img.get("src", "?")[:80]
            row["findings"].append({
                "label": f"Подозрительный alt: «{alt_val}»",
                "severity": "info",
                "context": f"<img src=\"{src}\" alt=\"{alt_val}\">",
                "source": "img_alt",
            })

    row["findings_count"] = len(row["findings"])
    return row


# ── Пакетный запуск ─────────────────────────────────────────────────────────

def check_many(pages: list[dict], *, verbose: bool = True) -> list[dict]:
    """
    Проверяет список страниц.
    pages — [{"url": ..., "resp"?: ..., "html"?: ...}, ...].
    Возвращает только страницы, на которых что-то найдено.
    """
    if verbose:
        print(f"  [{CHECK_NAME}] Ищу заглушки на {len(pages)} страницах...")

    results: list[dict] = []
    for page in pages:
        row = check(
            page["url"],
            resp=page.get("resp"),
            html=page.get("html"),
        )
        if row["findings"]:
            results.append(row)

    if verbose:
        total_findings = sum(r["findings_count"] for r in results)
        print(
            f"  [{CHECK_NAME}] Страниц с заглушками: {len(results)}, "
            f"всего находок: {total_findings}"
        )

    return results


# ── Фильтры ─────────────────────────────────────────────────────────────────

def filter_by_severity(results: list[dict], severity: str) -> list[dict]:
    """Оставляет только записи, где есть хотя бы одна находка с данным severity."""
    filtered = []
    for r in results:
        matching = [f for f in r["findings"] if f["severity"] == severity]
        if matching:
            filtered.append({**r, "findings": matching, "findings_count": len(matching)})
    return filtered


def filter_by_source(results: list[dict], source: str) -> list[dict]:
    """Фильтр по source: visible_text, title, description, html_comment, img_alt."""
    filtered = []
    for r in results:
        matching = [f for f in r["findings"] if f["source"] == source]
        if matching:
            filtered.append({**r, "findings": matching, "findings_count": len(matching)})
    return filtered


# ── Сводка ──────────────────────────────────────────────────────────────────

def summary(results: list[dict]) -> str:
    total_findings = sum(r["findings_count"] for r in results)
    lines = [
        f"[{CHECK_NAME}] Страниц с заглушками: {len(results)}, "
        f"всего находок: {total_findings}"
    ]

    for r in results:
        lines.append(f"  Страница: {r['url']}")
        for f in r["findings"]:
            sev = "⚠" if f["severity"] == "warning" else "ℹ"
            lines.append(f"    {sev} {f['label']}")
            lines.append(f"      контекст: {f['context']}")

    return "\n".join(lines)
