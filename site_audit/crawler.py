"""
Краулер: сбор URL через sitemap.xml и/или асинхронный обход по внутренним ссылкам.

Асинхронные функции:
  - async_try_sitemap — парсит sitemap.xml / sitemap_index.xml
  - async_crawl — BFS-обход сайта по внутренним ссылкам

Синхронные функции (только парсинг HTML, без сети):
  - extract_links — извлекает внутренние и внешние ссылки со страницы
  - extract_images — извлекает изображения со страницы
"""

from __future__ import annotations

import asyncio
from collections import deque
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from .utils import (
    AsyncResponse,
    async_fetch,
    create_aiohttp_session,
    get_domain,
    is_async_response_ok,
    is_same_domain,
    make_absolute,
    parse_html,
)

# Пространство имён для sitemap XML
_SITEMAP_NS: dict[str, str] = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Расширения файлов-ресурсов, которые не являются HTML-страницами
_ASSET_EXTENSIONS: frozenset[str] = frozenset((
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".gz", ".tar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".xml", ".json", ".csv", ".txt", ".rss",
))

# Максимальная глубина вложенности sitemap_index → sitemap
_MAX_SITEMAP_DEPTH: int = 3


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронный Sitemap
# ═════════════════════════════════════════════════════════════════════════════

async def async_try_sitemap(
    base_url: str,
    *,
    timeout: int = 15,
    verbose: bool = True,
) -> set[str]:
    """
    Асинхронно собирает URL из sitemap.xml / sitemap_index.xml.

    Пробует стандартные пути (/sitemap.xml, /sitemap_index.xml).
    Рекурсивно обрабатывает вложенные sitemap (до глубины 3).

    Args:
        base_url: корневой URL сайта.
        timeout: таймаут HTTP-запросов в секундах.
        verbose: выводить ли прогресс в консоль.

    Returns:
        Множество найденных URL страниц.
    """
    candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
    ]
    urls: set[str] = set()
    visited_sitemaps: set[str] = set()

    session = create_aiohttp_session(
        max_concurrent=10,
        timeout_total=timeout,
    )

    try:
        for candidate in candidates:
            await _parse_sitemap(
                candidate,
                session=session,
                urls=urls,
                visited=visited_sitemaps,
                timeout=timeout,
                depth=0,
            )
            if urls:
                break
    finally:
        await session.close()

    if verbose:
        if urls:
            print(f"  Sitemap: найдено {len(urls)} URL.")
        else:
            print("  Sitemap не найден или пуст.")

    return urls


async def _parse_sitemap(
    xml_url: str,
    *,
    session: "aiohttp.ClientSession",  # type: ignore[name-defined]
    urls: set[str],
    visited: set[str],
    timeout: int,
    depth: int,
) -> None:
    """
    Рекурсивно парсит один sitemap-файл.

    Если файл является sitemap_index — рекурсивно загружает
    вложенные карты сайта. Если обычный sitemap — извлекает URL страниц.

    Args:
        xml_url: URL файла sitemap.
        session: aiohttp-сессия.
        urls: множество для накопления найденных URL.
        visited: множество уже обработанных sitemap-URL.
        timeout: таймаут запроса.
        depth: текущая глубина рекурсии.
    """
    if depth > _MAX_SITEMAP_DEPTH or xml_url in visited:
        return
    visited.add(xml_url)

    resp = await async_fetch(
        xml_url,
        session=session,
        timeout=timeout,
        retries=1,
        retry_delay=1.0,
    )

    if not resp.ok or resp.status != 200:
        return

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return

    # sitemap_index → вложенные карты
    sub_locs = root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS)
    if sub_locs:
        tasks = []
        for loc in sub_locs:
            if loc.text:
                sub_url = loc.text.strip()
                tasks.append(
                    _parse_sitemap(
                        sub_url,
                        session=session,
                        urls=urls,
                        visited=visited,
                        timeout=timeout,
                        depth=depth + 1,
                    )
                )
        await asyncio.gather(*tasks)
        return

    # обычный sitemap → URL страниц
    for loc in root.findall(".//sm:url/sm:loc", _SITEMAP_NS):
        if loc.text:
            urls.add(loc.text.strip())


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронный BFS-обход
# ═════════════════════════════════════════════════════════════════════════════

async def async_crawl(
    base_url: str,
    *,
    max_pages: int = 500,
    max_depth: int = 3,
    max_concurrent: int = 20,
    timeout: int = 15,
    delay: float = 0.0,
    verbose: bool = True,
) -> list[str]:
    """
    Асинхронный BFS-обход сайта по внутренним ссылкам.

    Загружает страницы параллельно (ограничено семафором),
    извлекает внутренние ссылки и добавляет их в очередь.
    Работает послойно: сначала все URL глубины 0,
    затем все URL глубины 1 и т.д.

    Args:
        base_url: стартовый URL.
        max_pages: максимальное количество собранных URL.
        max_depth: максимальная глубина обхода.
        max_concurrent: количество одновременных запросов.
        timeout: таймаут HTTP-запросов в секундах.
        delay: задержка между запросами (для защиты от бана).
        verbose: выводить ли прогресс в консоль.

    Returns:
        Список URL в порядке обнаружения.
    """
    domain = get_domain(base_url)
    seen: set[str] = {base_url}
    collected: list[str] = [base_url]

    # Очередь послойного обхода: список URL текущего уровня глубины
    current_layer: list[str] = [base_url]

    semaphore = asyncio.Semaphore(max_concurrent)
    session = create_aiohttp_session(
        max_concurrent=max_concurrent,
        timeout_total=timeout,
    )

    try:
        for depth in range(max_depth):
            if not current_layer or len(collected) >= max_pages:
                break

            # Загружаем все URL текущего уровня параллельно
            responses = await _fetch_layer(
                current_layer,
                session=session,
                semaphore=semaphore,
                timeout=timeout,
                delay=delay,
            )

            next_layer: list[str] = []

            for resp in responses:
                if not is_async_response_ok(resp):
                    continue

                # Извлекаем ссылки из HTML
                links = extract_links(resp.text, resp.url)

                for link in links["internal"]:
                    if link in seen:
                        continue
                    if _is_asset(link):
                        continue
                    if not is_same_domain(link, domain):
                        continue

                    seen.add(link)
                    collected.append(link)
                    next_layer.append(link)

                    if len(collected) >= max_pages:
                        break

                if len(collected) >= max_pages:
                    break

            current_layer = next_layer

            if verbose and current_layer:
                print(
                    f"    Глубина {depth + 1}: найдено {len(current_layer)} новых URL "
                    f"(всего {len(collected)})."
                )
    finally:
        await session.close()

    if verbose:
        print(f"  Краулер: собрано {len(collected)} URL (глубина до {max_depth}).")

    return collected


async def _fetch_layer(
    urls: list[str],
    *,
    session: "aiohttp.ClientSession",  # type: ignore[name-defined]
    semaphore: asyncio.Semaphore,
    timeout: int,
    delay: float,
) -> list[AsyncResponse]:
    """
    Загружает все URL одного уровня глубины параллельно.

    Args:
        urls: список URL текущего слоя BFS.
        session: aiohttp-сессия.
        semaphore: ограничитель параллельности.
        timeout: таймаут запроса.
        delay: задержка между запросами.

    Returns:
        Список AsyncResponse для каждого URL.
    """

    async def _fetch_one(url: str) -> AsyncResponse:
        if delay > 0:
            await asyncio.sleep(delay)
        return await async_fetch(
            url,
            session=session,
            semaphore=semaphore,
            timeout=timeout,
            retries=1,
        )

    tasks = [_fetch_one(url) for url in urls]
    return await asyncio.gather(*tasks)


# ═════════════════════════════════════════════════════════════════════════════
# Извлечение ссылок и изображений (синхронные, без сети)
# ═════════════════════════════════════════════════════════════════════════════

def extract_links(html: str, page_url: str) -> dict[str, list[str]]:
    """
    Извлекает все ссылки <a href> со страницы.

    Разделяет на внутренние и внешние по домену.
    Игнорирует якоря (#), javascript:, mailto:, tel:.

    Args:
        html: HTML-код страницы.
        page_url: URL страницы (для определения домена и абсолютных ссылок).

    Returns:
        Словарь {"internal": [...], "external": [...]}.
    """
    domain = get_domain(page_url)
    soup = parse_html(html)
    internal: list[str] = []
    external: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = make_absolute(page_url, href)
        if is_same_domain(absolute, domain):
            internal.append(absolute)
        else:
            external.append(absolute)

    return {"internal": internal, "external": external}


def extract_images(html: str, page_url: str) -> list[dict[str, str | bool]]:
    """
    Извлекает все изображения <img> со страницы.

    Args:
        html: HTML-код страницы.
        page_url: URL страницы (для абсолютных ссылок).

    Returns:
        Список словарей {"src": ..., "alt": ..., "has_alt": True/False}.
    """
    soup = parse_html(html)
    images: list[dict[str, str | bool]] = []

    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        srcset = img.get("srcset", "").strip()

        # Берём первый src (или первый кандидат из srcset)
        if not src and srcset:
            src = srcset.split(",")[0].strip().split()[0]
        if not src:
            continue

        absolute = make_absolute(page_url, src)
        alt = img.get("alt")
        images.append({
            "src": absolute,
            "alt": alt,
            "has_alt": alt is not None,
        })

    return images


# ═════════════════════════════════════════════════════════════════════════════
# Вспомогательные
# ═════════════════════════════════════════════════════════════════════════════

def _is_asset(url: str) -> bool:
    """
    Грубая проверка: URL ведёт на файл-ресурс, а не HTML-страницу.

    Args:
        url: проверяемый URL.

    Returns:
        True, если URL заканчивается расширением файла-ресурса.
    """
    path = url.split("?")[0].lower()
    return any(path.endswith(ext) for ext in _ASSET_EXTENSIONS)
