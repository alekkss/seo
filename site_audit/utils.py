# site_audit/utils.py
"""
Общие утилиты: HTTP-клиент с повторами, хелперы для URL и парсинга.
"""

import time
import hashlib
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

TAGS_TO_STRIP = ["script", "style", "noscript", "nav", "header", "footer", "svg"]


# ── HTTP ─────────────────────────────────────────────────────────────────────

def fetch(url: str, *, timeout: int = 15, retries: int = 2,
          retry_delay: float = 2.0, method: str = "GET",
          allow_redirects: bool = True, session: requests.Session | None = None):
    """GET/HEAD-запрос с повторами при сетевых ошибках и 5xx."""
    requester = session or requests
    last: requests.Response | Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requester.request(
                method, url,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
            last = resp
            if resp.status_code < 500:
                return resp
        except requests.RequestException as exc:
            last = exc
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))
    return last


def head(url: str, **kwargs):
    """HEAD-запрос (для проверки ресурсов без скачивания тела)."""
    return fetch(url, method="HEAD", **kwargs)


# ── URL-хелперы ──────────────────────────────────────────────────────────────

def get_domain(url: str) -> str:
    return urlparse(url).netloc


def normalize_url(url: str) -> str:
    """Приводит URL к каноническому виду: убирает фрагмент, trailing slash."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, ""))


def is_same_domain(url: str, domain: str) -> bool:
    return urlparse(url).netloc == domain


def make_absolute(base: str, href: str) -> str:
    """Превращает относительную ссылку в абсолютную и отрезает фрагмент."""
    return urljoin(base, href).split("#")[0]


# ── Парсинг ──────────────────────────────────────────────────────────────────

def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def visible_text(soup: BeautifulSoup, *, strip_tags: list[str] | None = None) -> str:
    """Извлекает видимый текст из <body>, удалив служебные теги."""
    clone = BeautifulSoup(str(soup), "lxml")
    for tag_name in (strip_tags or TAGS_TO_STRIP):
        for tag in clone.find_all(tag_name):
            tag.decompose()
    body = clone.body
    return body.get_text(separator=" ", strip=True) if body else ""


def text_hash(text: str) -> str:
    """MD5-хеш нормализованного текста (для поиска дублей)."""
    normalized = " ".join(text.lower().split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


# ── Результаты ───────────────────────────────────────────────────────────────

def is_html_response(resp: requests.Response) -> bool:
    ct = resp.headers.get("Content-Type", "")
    return "text/html" in ct or "application/xhtml+xml" in ct
