# site_audit/checks/security.py
"""
Проверка: безопасность.

Что проверяется:
  1. Security-заголовки HTTP-ответа:
     - Strict-Transport-Security (HSTS)
     - Content-Security-Policy (CSP)
     - X-Content-Type-Options
     - X-Frame-Options
     - Referrer-Policy
     - Permissions-Policy
  2. Mixed content: страница на HTTPS подгружает ресурсы по HTTP.
  3. Чувствительные / служебные файлы, которые не должны быть публично доступны:
     - /.env, /.git/config, /wp-config.php.bak, /phpinfo.php,
       /server-status, /debug, /elmah.axd и т. д.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests

from ..utils import fetch, head, parse_html, is_html_response, HEADERS

CHECK_NAME = "security"
DESCRIPTION = "Заголовки безопасности, mixed content, чувствительные файлы"


# ── Эталонные заголовки ─────────────────────────────────────────────────────

EXPECTED_HEADERS: dict[str, dict] = {
    "Strict-Transport-Security": {
        "severity": "high",
        "hint": "Включите HSTS (напр. max-age=31536000; includeSubDomains)",
    },
    "Content-Security-Policy": {
        "severity": "medium",
        "hint": "Настройте CSP для защиты от XSS и инъекций",
    },
    "X-Content-Type-Options": {
        "severity": "medium",
        "hint": "Установите значение nosniff",
    },
    "X-Frame-Options": {
        "severity": "medium",
        "hint": "Установите DENY или SAMEORIGIN для защиты от clickjacking",
    },
    "Referrer-Policy": {
        "severity": "low",
        "hint": "Рекомендуется strict-origin-when-cross-origin или no-referrer",
    },
    "Permissions-Policy": {
        "severity": "low",
        "hint": "Ограничьте доступ к API браузера (камера, микрофон и т. д.)",
    },
}


# ── Чувствительные пути ─────────────────────────────────────────────────────

SENSITIVE_PATHS: list[dict] = [
    {"path": "/.env",                 "description": "Переменные окружения (пароли, ключи API)"},
    {"path": "/.git/config",          "description": "Конфигурация Git-репозитория"},
    {"path": "/.git/HEAD",            "description": "Git HEAD (утечка структуры репозитория)"},
    {"path": "/.svn/entries",         "description": "SVN-метаданные"},
    {"path": "/wp-config.php.bak",    "description": "Бэкап конфигурации WordPress"},
    {"path": "/wp-config.php.save",   "description": "Бэкап конфигурации WordPress"},
    {"path": "/phpinfo.php",          "description": "phpinfo() — раскрытие конфигурации сервера"},
    {"path": "/info.php",             "description": "phpinfo() — раскрытие конфигурации сервера"},
    {"path": "/server-status",        "description": "Apache server-status"},
    {"path": "/server-info",          "description": "Apache server-info"},
    {"path": "/.htaccess",            "description": "Конфигурация Apache"},
    {"path": "/.htpasswd",            "description": "Файл паролей Apache"},
    {"path": "/web.config",           "description": "Конфигурация IIS"},
    {"path": "/crossdomain.xml",      "description": "Flash crossdomain policy (устаревший)"},
    {"path": "/elmah.axd",            "description": "ELMAH error log (.NET)"},
    {"path": "/debug",                "description": "Отладочная страница"},
    {"path": "/debug/default/view",   "description": "Yii debug panel"},
    {"path": "/adminer.php",          "description": "Adminer — управление БД"},
    {"path": "/phpmyadmin/",          "description": "phpMyAdmin — управление БД"},
    {"path": "/backup.sql",           "description": "Дамп базы данных"},
    {"path": "/dump.sql",             "description": "Дамп базы данных"},
    {"path": "/db.sql",               "description": "Дамп базы данных"},
    {"path": "/.DS_Store",            "description": "macOS metadata (утечка структуры каталогов)"},
    {"path": "/Thumbs.db",            "description": "Windows metadata"},
    {"path": "/composer.json",        "description": "PHP-зависимости (раскрытие стека)"},
    {"path": "/package.json",         "description": "Node.js-зависимости (раскрытие стека)"},
    {"path": "/Dockerfile",           "description": "Docker-конфигурация"},
    {"path": "/docker-compose.yml",   "description": "Docker Compose конфигурация"},
]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Заголовки безопасности
# ═════════════════════════════════════════════════════════════════════════════

def check_headers(url: str, *,
                  resp: requests.Response | None = None) -> dict:
    """
    Проверяет security-заголовки одной страницы.

    Возвращает:
        {
            "check": "security",
            "type": "headers",
            "url": ...,
            "present": {"Header-Name": "value", ...},
            "missing": [{"header": ..., "severity": ..., "hint": ...}, ...],
        }
    """
    if resp is None:
        resp = fetch(url)

    result = {
        "check": CHECK_NAME,
        "type": "headers",
        "url": url,
        "present": {},
        "missing": [],
    }

    if isinstance(resp, Exception):
        result["missing"] = [
            {"header": h, **info} for h, info in EXPECTED_HEADERS.items()
        ]
        return result

    for header_name, info in EXPECTED_HEADERS.items():
        value = resp.headers.get(header_name)
        if value:
            result["present"][header_name] = value
        else:
            result["missing"].append({
                "header": header_name,
                "severity": info["severity"],
                "hint": info["hint"],
            })

    return result


# ═════════════════════════════════════════════════════════════════════════════
# 2. Mixed content
# ═════════════════════════════════════════════════════════════════════════════

_RESOURCE_ATTRS = [
    ("img", "src"),
    ("script", "src"),
    ("link", "href"),       # stylesheet, favicon и т. д.
    ("source", "src"),
    ("source", "srcset"),
    ("video", "src"),
    ("audio", "src"),
    ("iframe", "src"),
    ("object", "data"),
    ("embed", "src"),
]


def check_mixed_content(url: str, *,
                        resp: requests.Response | None = None,
                        html: str | None = None) -> list[dict]:
    """
    Находит ресурсы, подгружаемые по HTTP на HTTPS-странице.

    Возвращает список:
        [{"check": "security", "type": "mixed_content",
          "page_url": ..., "resource_url": ..., "tag": ..., "attr": ...}, ...]
    """
    page_scheme = urlparse(url).scheme
    if page_scheme != "https":
        return []

    if html is None:
        if resp is None:
            resp = fetch(url)
        if isinstance(resp, Exception) or not is_html_response(resp):
            return []
        html = resp.text

    soup = parse_html(html)
    issues: list[dict] = []

    for tag_name, attr in _RESOURCE_ATTRS:
        for tag in soup.find_all(tag_name):
            raw = tag.get(attr, "")
            if not raw:
                continue
            # srcset может содержать несколько URL через запятую
            urls_to_check = (
                [part.strip().split()[0] for part in raw.split(",")]
                if attr == "srcset"
                else [raw.strip()]
            )
            for res_url in urls_to_check:
                if not res_url:
                    continue
                absolute = urljoin(url, res_url)
                if urlparse(absolute).scheme == "http":
                    issues.append({
                        "check": CHECK_NAME,
                        "type": "mixed_content",
                        "page_url": url,
                        "resource_url": absolute,
                        "tag": tag_name,
                        "attr": attr,
                    })

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# 3. Чувствительные файлы
# ═════════════════════════════════════════════════════════════════════════════

def check_sensitive_files(base_url: str, *,
                          workers: int = 10,
                          timeout: int = 8,
                          verbose: bool = True) -> list[dict]:
    """
    Проверяет доступность чувствительных / служебных файлов.

    Считаем файл «открытым», если HEAD (или GET) возвращает 200
    и Content-Length > 0 (чтобы отсеять пустые заглушки).

    Возвращает:
        [{"check": "security", "type": "sensitive_file",
          "url": ..., "description": ..., "status_code": ..., "content_length": ...}, ...]
    """
    targets = [
        {**entry, "url": urljoin(base_url, entry["path"])}
        for entry in SENSITIVE_PATHS
    ]

    if verbose:
        print(f"  [{CHECK_NAME}] Проверяю {len(targets)} чувствительных путей...")

    results: list[dict] = []
    done = 0

    def _probe(target: dict) -> dict | None:
        url = target["url"]
        resp = head(url, timeout=timeout, retries=0)

        # fallback на GET при 405
        if isinstance(resp, requests.Response) and resp.status_code in (405, 501):
            resp = fetch(url, timeout=timeout, retries=0)

        if isinstance(resp, Exception):
            return None
        if resp.status_code != 200:
            return None

        cl = resp.headers.get("Content-Length", "0")
        content_length = int(cl) if cl.isdigit() else None

        # пустой ответ — скорее всего заглушка/редирект на главную
        if content_length is not None and content_length == 0:
            return None

        return {
            "check": CHECK_NAME,
            "type": "sensitive_file",
            "url": url,
            "path": target["path"],
            "description": target["description"],
            "status_code": resp.status_code,
            "content_length": content_length,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_probe, t): t for t in targets}
        for future in as_completed(futures):
            done += 1
            row = future.result()
            if row:
                results.append(row)

    if verbose:
        print(f"  [{CHECK_NAME}] Открытых чувствительных файлов: {len(results)}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Полная проверка
# ═════════════════════════════════════════════════════════════════════════════

def check(base_url: str,
          pages: list[dict], *,
          workers: int = 10,
          verbose: bool = True) -> list[dict]:
    """
    Запускает все три группы проверок и возвращает единый список проблем.

    pages — [{"url": ..., "resp"?: ..., "html"?: ...}, ...].
    """
    all_issues: list[dict] = []

    # 1. Заголовки (проверяем только главную + несколько страниц)
    sample_urls = [base_url] + [p["url"] for p in pages[:5]]
    for u in sample_urls:
        resp = next((p.get("resp") for p in pages if p["url"] == u), None)
        hdr = check_headers(u, resp=resp)
        if hdr["missing"]:
            all_issues.append(hdr)

    # 2. Mixed content (на всех страницах)
    for page in pages:
        mc = check_mixed_content(
            page["url"],
            resp=page.get("resp"),
            html=page.get("html"),
        )
        all_issues.extend(mc)

    # 3. Чувствительные файлы
    sf = check_sensitive_files(base_url, workers=workers, verbose=verbose)
    all_issues.extend(sf)

    return all_issues


# ── Сводка ──────────────────────────────────────────────────────────────────

def summary(results: list[dict]) -> str:
    headers_issues = [r for r in results if r.get("type") == "headers"]
    mixed = [r for r in results if r.get("type") == "mixed_content"]
    sensitive = [r for r in results if r.get("type") == "sensitive_file"]

    lines = [
        f"[{CHECK_NAME}] "
        f"Заголовки: {len(headers_issues)} страниц с проблемами, "
        f"Mixed content: {len(mixed)}, "
        f"Чувствительные файлы: {len(sensitive)}"
    ]

    if headers_issues:
        lines.append("  Заголовки безопасности:")
        for r in headers_issues:
            missing_names = [m["header"] for m in r["missing"]]
            lines.append(f"    ⚠ {r['url']}  —  нет: {', '.join(missing_names)}")

    if mixed:
        lines.append("  Mixed content:")
        for r in mixed:
            lines.append(f"    ✗ {r['page_url']}  загружает  {r['resource_url']}  (<{r['tag']} {r['attr']}>)")

    if sensitive:
        lines.append("  Чувствительные файлы (HTTP 200):")
        for r in sensitive:
            size = f" ({r['content_length']} байт)" if r.get("content_length") else ""
            lines.append(f"    ✗ {r['url']}{size}  —  {r['description']}")

    return "\n".join(lines)
