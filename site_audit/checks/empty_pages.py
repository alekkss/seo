# site_audit/checks/empty_pages.py
"""
Проверка: пустые или почти пустые страницы.

Критерии «пустоты»:
  - HTTP-статус ≠ 200
  - Отсутствует тег <body>
  - Количество видимого текста в <body> ниже порога
  - Ошибка при запросе (таймаут, сетевая ошибка)
"""

from __future__ import annotations

import requests

from ..utils import fetch, parse_html, visible_text, is_html_response

CHECK_NAME = "empty_pages"
DESCRIPTION = "Пустые или почти пустые страницы"


# ── Основная функция проверки ────────────────────────────────────────────────

def check(url: str, *,
          resp: requests.Response | None = None,
          html: str | None = None,
          min_text_length: int = 100) -> dict:
    """
    Проверяет одну страницу.

    Параметры
    ---------
    url : str
        Адрес страницы.
    resp : requests.Response | None
        Уже полученный ответ (чтобы не делать повторный запрос).
        Если None — страница будет скачана.
    html : str | None
        Готовый HTML (если resp уже распарсен выше по цепочке).
    min_text_length : int
        Минимальное количество символов видимого текста,
        ниже которого страница считается пустой.

    Возвращает
    ----------
    dict с полями:
        check       — имя проверки
        url         — исходный URL
        status_code — HTTP-статус (None при ошибке сети)
        final_url   — URL после редиректов
        html_size   — размер HTML в байтах
        text_length — количество символов видимого текста
        is_empty    — True, если страница признана пустой
        reason      — причина (пустая строка, если всё ок)
    """
    row = _blank_row(url)

    # ── Получаем ответ, если не передан ─────────────────────────────────
    if resp is None:
        resp = fetch(url)

    if isinstance(resp, Exception):
        row["is_empty"] = True
        row["reason"] = f"Ошибка запроса: {resp}"
        return row

    row["status_code"] = resp.status_code
    row["final_url"] = resp.url
    row["html_size"] = len(resp.content)

    # ── Не-200 ──────────────────────────────────────────────────────────
    if resp.status_code != 200:
        row["is_empty"] = True
        row["reason"] = f"HTTP {resp.status_code}"
        return row

    # ── Не HTML ─────────────────────────────────────────────────────────
    if not is_html_response(resp):
        row["reason"] = "Не HTML (пропущено)"
        return row

    # ── Анализ контента ─────────────────────────────────────────────────
    source = html if html is not None else resp.text
    soup = parse_html(source)
    body = soup.body

    if body is None:
        row["is_empty"] = True
        row["reason"] = "Нет тега <body>"
        return row

    text = visible_text(soup)
    row["text_length"] = len(text)

    if len(text) < min_text_length:
        row["is_empty"] = True
        row["reason"] = (
            f"Мало текста ({len(text)} симв., порог {min_text_length})"
        )

    return row


# ── Пакетный запуск по списку результатов ────────────────────────────────────

def check_many(pages: list[dict], *, min_text_length: int = 100) -> list[dict]:
    """
    Принимает список словарей вида {"url": ..., "resp": ..., "html": ...}
    (resp и html опциональны) и возвращает список результатов проверки.
    """
    results: list[dict] = []
    for page in pages:
        row = check(
            page["url"],
            resp=page.get("resp"),
            html=page.get("html"),
            min_text_length=min_text_length,
        )
        results.append(row)
    return results


# ── Фильтрация ──────────────────────────────────────────────────────────────

def filter_empty(results: list[dict]) -> list[dict]:
    """Возвращает только записи, где is_empty=True."""
    return [r for r in results if r.get("is_empty")]


def summary(results: list[dict]) -> str:
    """Текстовая сводка для консоли."""
    total = len(results)
    empty = filter_empty(results)
    lines = [
        f"[{CHECK_NAME}] Проверено: {total}, пустых: {len(empty)}",
    ]
    for r in empty:
        lines.append(f"  ✗ {r['url']}  —  {r['reason']}")
    return "\n".join(lines)


# ── Вспомогательные ─────────────────────────────────────────────────────────

def _blank_row(url: str) -> dict:
    return {
        "check": CHECK_NAME,
        "url": url,
        "status_code": None,
        "final_url": None,
        "html_size": 0,
        "text_length": 0,
        "is_empty": False,
        "reason": "",
    }
