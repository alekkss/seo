"""
Краулер: сбор URL через sitemap.xml и/или асинхронный обход по внутренним ссылкам.
Асинхронные функции:
    async_try_sitemap — парсит sitemap.xml / sitemap_index.xml
    async_crawl — BFS-обход сайта по внутренним ссылкам
Синхронные функции (только парсинг HTML, без сети):
    extract_links — извлекает внутренние и внешние ссылки со страницы
    extract_images — извлекает изображения со страницы
Фабрика:
    create_proxy_rotator — создаёт ProxyRotator из настроек приложения
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from .config.logger import get_logger
from .config.settings import get_settings
from .proxy import ProxyRotator
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

if TYPE_CHECKING:
    import aiohttp

logger = get_logger("crawler")

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

# Схемы URL, которые не являются HTTP-ссылками и не подлежат проверке.
# data: — inline-ресурсы в base64, blob: — объекты в памяти браузера,
# javascript: — псевдо-ссылки для скриптов.
_NON_HTTP_SCHEMES: tuple[str, ...] = ("data:", "blob:", "javascript:")


# ═════════════════════════════════════════════════════════════════════════════
# Фабрика ProxyRotator
# ═════════════════════════════════════════════════════════════════════════════

def create_proxy_rotator() -> ProxyRotator | None:
    """
    Создаёт ProxyRotator из настроек приложения.
    Единая точка инициализации для CLI, бота и библиотечного использования.
    Если прокси отключены в настройках — возвращает None.

    Returns:
        Настроенный ProxyRotator или None, если прокси отключены.
    """
    settings = get_settings()

    if not settings.proxy_enabled:
        logger.info("Прокси отключены в настройках (PROXY_ENABLED=false)")
        return None

    rotator = ProxyRotator.from_file(
        settings.proxy_file_path,
        cooldown=settings.proxy_cooldown,
        max_fails=settings.proxy_max_fails,
        max_connections=settings.proxy_max_connections,
    )

    if not rotator.is_enabled:
        logger.warning(
            "Прокси включены в настройках, но файл пуст или не найден",
            extra={"context": {"path": settings.proxy_file_path}},
        )
        return None

    return rotator


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронный Sitemap
# ═════════════════════════════════════════════════════════════════════════════

async def async_try_sitemap(
    base_url: str,
    *,
    timeout: int | None = None,
    retries: int | None = None,
    proxy_rotator: ProxyRotator | None = None,
    verbose: bool = True,
) -> set[str]:
    """
    Асинхронно собирает URL из sitemap.xml / sitemap_index.xml.
    Пробует стандартные пути (/sitemap.xml, /sitemap_index.xml).
    Рекурсивно обрабатывает вложенные sitemap (до глубины 3).
    Использует настройки из get_settings() для таймаутов и retries.

    Args:
        base_url: корневой URL сайта.
        timeout: таймаут HTTP-запросов в секундах (None — из настроек).
        retries: количество повторов при ошибках (None — из настроек).
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        verbose: выводить ли прогресс в консоль.

    Returns:
        Множество найденных URL страниц.
    """
    settings = get_settings()
    _timeout = timeout if timeout is not None else settings.default_timeout
    _retries = retries if retries is not None else settings.default_retries

    candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
    ]
    urls: set[str] = set()
    visited_sitemaps: set[str] = set()

    session = create_aiohttp_session(
        max_concurrent=10,
        timeout_total=_timeout,
    )
    try:
        for candidate in candidates:
            await _parse_sitemap(
                candidate,
                session=session,
                urls=urls,
                visited=visited_sitemaps,
                timeout=_timeout,
                retries=_retries,
                proxy_rotator=proxy_rotator,
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
    session: aiohttp.ClientSession,
    urls: set[str],
    visited: set[str],
    timeout: int,
    retries: int,
    proxy_rotator: ProxyRotator | None,
    depth: int,
) -> None:
    """
    Рекурсивно парсит один sitemap-файл.
    Если файл является sitemap_index — рекурсивно загружает
    вложенные карты сайта. Если обычный sitemap — извлекает URL страниц.
    Все ошибки логируются для диагностики.

    Args:
        xml_url: URL файла sitemap.
        session: aiohttp-сессия.
        urls: множество для накопления найденных URL.
        visited: множество уже обработанных sitemap-URL.
        timeout: таймаут запроса.
        retries: количество повторов.
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        depth: текущая глубина рекурсии.
    """
    if depth > _MAX_SITEMAP_DEPTH or xml_url in visited:
        return
    visited.add(xml_url)

    resp = await async_fetch(
        xml_url,
        session=session,
        timeout=timeout,
        retries=retries,
        retry_delay=1.5,
        proxy_rotator=proxy_rotator,
    )

    if not resp.ok or resp.status != 200:
        logger.warning(
            "Не удалось загрузить sitemap",
            extra={"context": {
                "url": xml_url,
                "status": resp.status,
                "error": resp.error,
            }},
        )
        return

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning(
            "Ошибка парсинга XML sitemap",
            extra={"context": {
                "url": xml_url,
                "error": str(exc),
            }},
        )
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
                        retries=retries,
                        proxy_rotator=proxy_rotator,
                        depth=depth + 1,
                    )
                )
        # CRITICAL: return_exceptions=True, чтобы один таймаут sitemap
        # не ломал обработку остальных вложенных карт
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "Исключение при парсинге вложенного sitemap",
                    extra={"context": {
                        "error_type": type(result).__name__,
                        "error": str(result),
                    }},
                )
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
    timeout: int | None = None,
    retries: int | None = None,
    delay: float = 0.0,
    proxy_rotator: ProxyRotator | None = None,
    verbose: bool = True,
) -> list[str]:
    """
    Асинхронный BFS-обход сайта по внутренним ссылкам.
    Загружает страницы параллельно (ограничено семафором),
    извлекает внутренние ссылки и добавляет их в очередь.
    Работает послойно: сначала все URL глубины 0,
    затем все URL глубины 1 и т.д.
    Использует настройки из get_settings() для таймаутов и retries.

    Args:
        base_url: стартовый URL.
        max_pages: максимальное количество собранных URL.
        max_depth: максимальная глубина обхода.
        max_concurrent: количество одновременных запросов.
        timeout: таймаут HTTP-запросов в секундах (None — из настроек).
        retries: количество повторов при ошибках (None — из настроек).
        delay: задержка между запросами (для защиты от бана).
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        verbose: выводить ли прогресс в консоль.

    Returns:
        Список URL в порядке обнаружения.
    """
    settings = get_settings()
    _timeout = timeout if timeout is not None else settings.default_timeout
    _retries = retries if retries is not None else settings.default_retries

    domain = get_domain(base_url)
    seen: set[str] = {base_url}
    collected: list[str] = [base_url]

    # Очередь послойного обхода: список URL текущего уровня глубины
    current_layer: list[str] = [base_url]
    semaphore = asyncio.Semaphore(max_concurrent)

    session = create_aiohttp_session(
        max_concurrent=max_concurrent,
        timeout_total=_timeout,
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
                timeout=_timeout,
                retries=_retries,
                delay=delay,
                proxy_rotator=proxy_rotator,
            )

            next_layer: list[str] = []
            for resp in responses:
                if not is_async_response_ok(resp):
                    # Логируем ошибки загрузки для диагностики
                    if resp.error:
                        logger.debug(
                            "Страница не загружена при обходе",
                            extra={"context": {
                                "url": resp.url,
                                "error": resp.error,
                            }},
                        )
                    continue

                # Извлекаем ссылки из HTML с защитой от ошибок парсинга
                try:
                    links = extract_links(resp.text, resp.url)
                except Exception as exc:
                    logger.warning(
                        "Ошибка парсинга HTML при обходе",
                        extra={"context": {
                            "url": resp.url,
                            "error": str(exc),
                        }},
                    )
                    continue

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
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    timeout: int,
    retries: int,
    delay: float,
    proxy_rotator: ProxyRotator | None = None,
) -> list[AsyncResponse]:
    """
    Загружает все URL одного уровня глубины параллельно.
    CRITICAL: return_exceptions=True — чтобы один таймаут
    не ломал загрузку всего слоя BFS. Исключения оборачиваются
    в AsyncResponse с описанием ошибки.

    Args:
        urls: список URL текущего слоя BFS.
        session: aiohttp-сессия.
        semaphore: ограничитель параллельности.
        timeout: таймаут запроса.
        retries: количество повторов.
        delay: задержка между запросами.
        proxy_rotator: ротатор прокси (None — запросы напрямую).

    Returns:
        Список AsyncResponse для каждого URL.
    """
    async def _fetch_one(url: str) -> AsyncResponse:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await async_fetch(
                url,
                session=session,
                semaphore=semaphore,
                timeout=timeout,
                retries=retries,
                retry_delay=1.5,
                proxy_rotator=proxy_rotator,
            )
        except Exception as exc:
            # Защита от неожиданных исключений — оборачиваем в AsyncResponse
            logger.error(
                "Неожиданная ошибка при загрузке слоя",
                extra={"context": {
                    "url": url,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }},
            )
            return AsyncResponse(
                url=url,
                error=f"Ошибка задачи: {type(exc).__name__}: {exc}",
                ok=False,
            )

    tasks = [_fetch_one(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Оборачиваем любые исключения из gather в AsyncResponse
    final_results: list[AsyncResponse] = []
    for url, result in zip(urls, results):
        if isinstance(result, AsyncResponse):
            final_results.append(result)
        elif isinstance(result, Exception):
            logger.error(
                "Исключение из gather при загрузке слоя",
                extra={"context": {
                    "url": url,
                    "error_type": type(result).__name__,
                    "error": str(result),
                }},
            )
            final_results.append(AsyncResponse(
                url=url,
                error=f"Ошибка задачи: {type(result).__name__}: {result}",
                ok=False,
            ))
        else:
            final_results.append(AsyncResponse(
                url=url,
                error=f"Неизвестный результат задачи: {type(result).__name__}",
                ok=False,
            ))

    return final_results


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
    Пропускает не-HTTP URL: data: (inline base64), blob: (объекты
    в памяти браузера), javascript: (псевдо-ссылки) — они не подлежат
    проверке через HEAD-запрос и вызывают ошибки aiohttp.

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

        # Пропускаем не-HTTP схемы: data:, blob:, javascript:
        # Эти URL не являются сетевыми ресурсами и не подлежат проверке
        if src.startswith(_NON_HTTP_SCHEMES):
            continue

        absolute = make_absolute(page_url, src)

        # Дополнительная проверка: после приведения к абсолютному URL
        # он должен начинаться с http:// или https://
        if not absolute.startswith(("http://", "https://")):
            continue

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
