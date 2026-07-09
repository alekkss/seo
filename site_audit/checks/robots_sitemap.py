"""
Проверка: robots.txt и валидация sitemap.

Что проверяется в robots.txt:
  - Существование файла robots.txt
  - Блокировка важных путей (/, /css/, /js/, /images/)
  - Наличие директивы Sitemap
  - Директива Disallow: / (полная блокировка сайта)
  - Пустой robots.txt (есть файл, но нет правил)

Что проверяется в sitemap:
  - URL из sitemap, которые отвечают не 200 (битые URL в sitemap)
  - URL из sitemap с noindex (противоречие: есть в sitemap, но запрещены)
  - Количество URL в sitemap для статистики

Два режима вызова:
  - check(...) — синхронная обёртка (для обратной совместимости)
  - async_check(...) — асинхронная функция (для audit_service)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from ..utils import (
    AsyncResponse,
    async_fetch,
    create_aiohttp_session,
    is_html_response,
    parse_html,
)

if TYPE_CHECKING:
    from ..proxy import ProxyRotator

CHECK_NAME = "robots_sitemap"
DESCRIPTION = "Проверка robots.txt и валидация sitemap"

# Максимальное количество URL из sitemap для проверки на noindex
# (проверять все URL нет смысла — они уже загружены в pages)
_MAX_SITEMAP_NOINDEX_CHECK: int = 500

# Важные пути, блокировка которых — проблема
_IMPORTANT_PATHS: tuple[str, ...] = (
    "/",
    "/css",
    "/js",
    "/images",
    "/img",
    "/assets",
    "/static",
    "/fonts",
    "/media",
)

# Прогресс выводится каждые N проверенных URL
_PROGRESS_EVERY: int = 100


# ═════════════════════════════════════════════════════════════════════════════
# Парсинг robots.txt
# ═════════════════════════════════════════════════════════════════════════════

def _parse_robots_txt(text: str) -> dict[str, Any]:
    """
    Парсит содержимое robots.txt в структурированный формат.

    Извлекает:
      - user_agents: словарь {user_agent: [правила]}
      - sitemaps: список URL из директив Sitemap
      - has_rules: есть ли хотя бы одно правило

    Args:
        text: содержимое файла robots.txt.

    Returns:
        Словарь с результатами парсинга.
    """
    result: dict[str, Any] = {
        "user_agents": {},
        "sitemaps": [],
        "has_rules": False,
        "raw_lines": 0,
    }

    current_ua: str = ""
    lines = text.strip().splitlines()
    result["raw_lines"] = len(lines)

    for raw_line in lines:
        line = raw_line.strip()

        # Пропускаем пустые строки и комментарии
        if not line or line.startswith("#"):
            continue

        # Разделяем по первому двоеточию
        if ":" not in line:
            continue

        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()

        if directive == "user-agent":
            current_ua = value.lower()
            if current_ua not in result["user_agents"]:
                result["user_agents"][current_ua] = []

        elif directive == "sitemap":
            if value:
                result["sitemaps"].append(value)

        elif directive in ("disallow", "allow"):
            if current_ua:
                result["user_agents"][current_ua].append({
                    "directive": directive,
                    "path": value,
                })
                result["has_rules"] = True

        elif directive == "crawl-delay":
            if current_ua:
                result["user_agents"][current_ua].append({
                    "directive": "crawl-delay",
                    "path": value,
                })
                result["has_rules"] = True

    return result


def _analyze_robots(
    parsed: dict[str, Any],
    base_url: str,
) -> list[dict[str, Any]]:
    """
    Анализирует распарсенный robots.txt и находит проблемы.

    Args:
        parsed: результат _parse_robots_txt().
        base_url: корневой URL сайта.

    Returns:
        Список найденных проблем.
    """
    issues: list[dict[str, Any]] = []

    # ── Пустой robots.txt ───────────────────────────────────────────────
    if not parsed["has_rules"] and not parsed["sitemaps"]:
        issues.append(_make_issue(
            issue_type="empty_robots",
            message="robots.txt существует, но не содержит правил",
            detail="Рекомендуется добавить хотя бы базовые директивы и ссылку на Sitemap",
        ))

    # ── Полная блокировка сайта ─────────────────────────────────────────
    for ua, rules in parsed["user_agents"].items():
        for rule in rules:
            if rule["directive"] == "disallow" and rule["path"] == "/":
                ua_label = ua if ua != "*" else "* (все боты)"
                issues.append(_make_issue(
                    issue_type="full_block",
                    message=f"Disallow: / для User-agent: {ua_label} — сайт полностью закрыт",
                    detail=(
                        "Поисковые роботы не смогут индексировать ни одну страницу. "
                        "Если это не намеренно — уберите эту директиву."
                    ),
                ))

    # ── Блокировка важных путей ─────────────────────────────────────────
    # Проверяем правила для * и googlebot
    check_uas = {"*", "googlebot", "yandex", "yandexbot"}
    for ua in check_uas:
        rules = parsed["user_agents"].get(ua, [])
        for rule in rules:
            if rule["directive"] != "disallow":
                continue
            path = rule["path"]
            if not path or path == "/":
                continue
            # Нормализуем путь для сравнения
            norm_path = path.rstrip("/").lower()
            for important in _IMPORTANT_PATHS:
                if important == "/":
                    continue
                if norm_path == important or norm_path.startswith(important + "/"):
                    ua_label = ua if ua != "*" else "* (все боты)"
                    issues.append(_make_issue(
                        issue_type="blocked_path",
                        message=(
                            f"Заблокирован важный путь: Disallow: {path} "
                            f"(User-agent: {ua_label})"
                        ),
                        detail=(
                            f"Блокировка {path} может помешать загрузке CSS/JS/изображений, "
                            f"что ухудшит отображение страниц в поиске."
                        ),
                    ))

    # ── Нет директивы Sitemap ───────────────────────────────────────────
    if not parsed["sitemaps"]:
        issues.append(_make_issue(
            issue_type="no_sitemap_directive",
            message="В robots.txt отсутствует директива Sitemap",
            detail=(
                "Рекомендуется добавить: Sitemap: "
                f"{urljoin(base_url, '/sitemap.xml')}"
            ),
        ))

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# Проверка noindex в страницах из sitemap
# ═════════════════════════════════════════════════════════════════════════════

def _check_sitemap_noindex(
    pages: list[dict[str, Any]],
    sitemap_urls: set[str] | None,
) -> list[dict[str, Any]]:
    """
    Находит URL из sitemap, у которых стоит noindex.
    Противоречие: страница заявлена в sitemap для индексации,
    но мета-тег запрещает индексацию.

    Args:
        pages: загруженные страницы.
        sitemap_urls: множество URL из sitemap.

    Returns:
        Список проблем.
    """
    if not sitemap_urls:
        return []

    issues: list[dict[str, Any]] = []

    for page in pages:
        url = page["url"]

        # Проверяем только страницы из sitemap
        if url not in sitemap_urls:
            continue

        html = page.get("html")
        if html is None:
            resp = page.get("resp")
            if resp is None or isinstance(resp, Exception):
                continue

            # Проверяем HTTP-заголовок X-Robots-Tag
            if isinstance(resp, AsyncResponse):
                x_robots = resp.header("X-Robots-Tag", "").lower()
            else:
                x_robots = resp.headers.get("X-Robots-Tag", "").lower()

            if "noindex" in x_robots:
                issues.append(_make_issue(
                    issue_type="sitemap_noindex",
                    message=f"URL из sitemap имеет X-Robots-Tag: noindex",
                    detail=url,
                ))
            continue

        # Проверяем мета-тег robots
        soup = parse_html(html)
        robots_tag = soup.find("meta", attrs={"name": "robots"})
        if robots_tag:
            content = (robots_tag.get("content") or "").lower()
            if "noindex" in content:
                issues.append(_make_issue(
                    issue_type="sitemap_noindex",
                    message="URL из sitemap имеет meta robots noindex",
                    detail=url,
                ))
                continue

        # Проверяем X-Robots-Tag в заголовке ответа
        resp = page.get("resp")
        if resp is not None and not isinstance(resp, Exception):
            if isinstance(resp, AsyncResponse):
                x_robots = resp.header("X-Robots-Tag", "").lower()
            else:
                x_robots = resp.headers.get("X-Robots-Tag", "").lower()
            if "noindex" in x_robots:
                issues.append(_make_issue(
                    issue_type="sitemap_noindex",
                    message="URL из sitemap имеет X-Robots-Tag: noindex",
                    detail=url,
                ))

    return issues


def _check_sitemap_non_200(
    pages: list[dict[str, Any]],
    sitemap_urls: set[str] | None,
) -> list[dict[str, Any]]:
    """
    Находит URL из sitemap, которые отвечают не 200.

    Args:
        pages: загруженные страницы.
        sitemap_urls: множество URL из sitemap.

    Returns:
        Список проблем.
    """
    if not sitemap_urls:
        return []

    issues: list[dict[str, Any]] = []

    for page in pages:
        url = page["url"]
        if url not in sitemap_urls:
            continue

        resp = page.get("resp")
        if resp is None or isinstance(resp, Exception):
            error_msg = str(resp) if isinstance(resp, Exception) else "нет ответа"
            issues.append(_make_issue(
                issue_type="sitemap_error",
                message=f"URL из sitemap недоступен: {error_msg}",
                detail=url,
            ))
            continue

        status = resp.status if isinstance(resp, AsyncResponse) else resp.status_code
        if status != 200:
            issues.append(_make_issue(
                issue_type="sitemap_non_200",
                message=f"URL из sitemap отвечает HTTP {status}",
                detail=url,
            ))

    return issues


# ═════════════════════════════════════════════════════════════════════════════
# Вспомогательные
# ═════════════════════════════════════════════════════════════════════════════

def _make_issue(
    *,
    issue_type: str,
    message: str,
    detail: str,
) -> dict[str, Any]:
    """
    Формирует словарь проблемы в едином формате.

    Args:
        issue_type: тип проблемы.
        message: описание проблемы.
        detail: URL или дополнительная информация.

    Returns:
        Словарь проблемы.
    """
    return {
        "check": CHECK_NAME,
        "issue_type": issue_type,
        "message": message,
        "detail": detail,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронная проверка (основная точка входа)
# ═════════════════════════════════════════════════════════════════════════════

async def async_check(
    pages: list[dict[str, Any]],
    *,
    base_url: str,
    sitemap_urls: set[str] | None = None,
    proxy_rotator: ProxyRotator | None = None,
    timeout: int = 30,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Асинхронная проверка robots.txt и валидация sitemap.

    Этапы:
      1. Загружает robots.txt через async_fetch.
      2. Парсит и анализирует содержимое.
      3. Проверяет URL из sitemap на non-200 и noindex.

    Args:
        pages: загруженные страницы.
        base_url: корневой URL сайта.
        sitemap_urls: множество URL из sitemap (None — пропускаем проверки sitemap).
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        timeout: таймаут HTTP-запроса в секундах.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с проблемами.
    """
    results: list[dict[str, Any]] = []

    if verbose:
        print(f"  [{CHECK_NAME}] Загружаю robots.txt...")

    # ── Шаг 1: загрузка robots.txt ──────────────────────────────────────
    robots_url = urljoin(base_url, "/robots.txt")

    session = create_aiohttp_session(
        max_concurrent=5,
        timeout_total=timeout + 10,
        timeout_connect=min(timeout, 10),
    )

    try:
        resp = await async_fetch(
            robots_url,
            session=session,
            proxy_rotator=proxy_rotator,
            timeout=timeout,
            retries=2,
            retry_delay=2.0,
        )
    finally:
        await session.close()

    # ── Шаг 2: анализ robots.txt ───────────────────────────────────────
    if not resp.ok or resp.status != 200:
        results.append(_make_issue(
            issue_type="no_robots",
            message=f"robots.txt не найден (HTTP {resp.status})",
            detail=(
                "Рекомендуется создать файл robots.txt с базовыми правилами "
                "и ссылкой на Sitemap."
            ),
        ))
    else:
        if verbose:
            print(f"  [{CHECK_NAME}] Анализирую robots.txt...")

        parsed = _parse_robots_txt(resp.text)
        robots_issues = _analyze_robots(parsed, base_url)
        results.extend(robots_issues)

        # Статистика robots.txt
        if verbose:
            ua_count = len(parsed["user_agents"])
            sm_count = len(parsed["sitemaps"])
            print(
                f"  [{CHECK_NAME}] robots.txt: "
                f"{parsed['raw_lines']} строк, "
                f"{ua_count} User-Agent, "
                f"{sm_count} Sitemap"
            )

    # ── Шаг 3: проверка URL из sitemap ──────────────────────────────────
    if sitemap_urls:
        if verbose:
            print(
                f"  [{CHECK_NAME}] Проверяю {len(sitemap_urls)} URL из sitemap "
                f"на non-200 и noindex..."
            )

        non_200 = _check_sitemap_non_200(pages, sitemap_urls)
        results.extend(non_200)

        noindex = _check_sitemap_noindex(pages, sitemap_urls)
        results.extend(noindex)

    if verbose:
        print(f"  [{CHECK_NAME}] Найдено проблем: {len(results)}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Синхронная обёртка (обратная совместимость)
# ═════════════════════════════════════════════════════════════════════════════

def check(
    pages: list[dict[str, Any]],
    *,
    base_url: str,
    sitemap_urls: set[str] | None = None,
    proxy_rotator: ProxyRotator | None = None,
    timeout: int = 30,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """
    Синхронная обёртка над async_check.

    Args:
        pages: загруженные страницы.
        base_url: корневой URL сайта.
        sitemap_urls: множество URL из sitemap.
        proxy_rotator: ротатор прокси.
        timeout: таймаут HTTP-запроса.
        verbose: выводить ли прогресс.

    Returns:
        Список словарей с проблемами.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            async_check(
                pages,
                base_url=base_url,
                sitemap_urls=sitemap_urls,
                proxy_rotator=proxy_rotator,
                timeout=timeout,
                verbose=verbose,
            )
        )
    finally:
        loop.close()


# ═════════════════════════════════════════════════════════════════════════════
# Фильтры
# ═════════════════════════════════════════════════════════════════════════════

def filter_robots_issues(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только проблемы robots.txt."""
    robots_types = {"no_robots", "empty_robots", "full_block", "blocked_path",
                    "no_sitemap_directive"}
    return [r for r in results if r["issue_type"] in robots_types]


def filter_sitemap_issues(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает только проблемы URL из sitemap."""
    sitemap_types = {"sitemap_non_200", "sitemap_noindex", "sitemap_error"}
    return [r for r in results if r["issue_type"] in sitemap_types]


# ═════════════════════════════════════════════════════════════════════════════
# Сводка
# ═════════════════════════════════════════════════════════════════════════════

_ISSUE_TYPE_LABELS: dict[str, str] = {
    "no_robots": "robots.txt не найден",
    "empty_robots": "robots.txt пуст",
    "full_block": "Полная блокировка сайта",
    "blocked_path": "Заблокирован важный путь",
    "no_sitemap_directive": "Нет директивы Sitemap",
    "sitemap_non_200": "URL из sitemap не отвечает 200",
    "sitemap_noindex": "URL из sitemap с noindex",
    "sitemap_error": "URL из sitemap недоступен",
}


def summary(results: list[dict[str, Any]]) -> str:
    """Текстовая сводка для консоли и отчёта."""
    robots = filter_robots_issues(results)
    sitemap = filter_sitemap_issues(results)

    lines = [
        f"[{CHECK_NAME}] Найдено: {len(results)} "
        f"(robots.txt: {len(robots)}, sitemap: {len(sitemap)})"
    ]

    if robots:
        lines.append("  Проблемы robots.txt:")
        for r in robots:
            label = _ISSUE_TYPE_LABELS.get(r["issue_type"], r["issue_type"])
            lines.append(f"    ✗ {label}: {r['message']}")
            if r["detail"] and r["issue_type"] != "no_robots":
                lines.append(f"        {r['detail']}")

    if sitemap:
        lines.append("  Проблемы URL из sitemap:")
        for r in sitemap[:20]:
            lines.append(f"    ✗ {r['message']}")
            lines.append(f"        {r['detail']}")
        if len(sitemap) > 20:
            lines.append(f"        ...и ещё {len(sitemap) - 20}")

    return "\n".join(lines)
