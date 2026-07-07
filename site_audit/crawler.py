# site_audit/crawler.py
"""
Краулер: сбор URL через sitemap.xml и/или обход по внутренним ссылкам.
"""

from collections import deque
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from .utils import fetch, get_domain, make_absolute, is_same_domain, is_html_response, parse_html


# ── Sitemap ──────────────────────────────────────────────────────────────────

def try_sitemap(base_url: str, *, verbose: bool = True) -> set[str]:
    """Пытается собрать URL из sitemap.xml / sitemap_index.xml."""
    candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
    ]
    urls: set[str] = set()
    visited_sitemaps: set[str] = set()

    def _parse(xml_url: str, depth: int = 0):
        if depth > 3 or xml_url in visited_sitemaps:
            return
        visited_sitemaps.add(xml_url)

        resp = fetch(xml_url)
        if isinstance(resp, Exception) or resp.status_code != 200:
            return
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        # sitemap index → вложенные карты
        sub = root.findall(".//sm:sitemap/sm:loc", ns)
        if sub:
            for loc in sub:
                if loc.text:
                    _parse(loc.text.strip(), depth + 1)
            return

        # обычный sitemap → URL страниц
        for loc in root.findall(".//sm:url/sm:loc", ns):
            if loc.text:
                urls.add(loc.text.strip())

    for c in candidates:
        _parse(c)
        if urls:
            break

    if verbose:
        if urls:
            print(f"  Sitemap: найдено {len(urls)} URL.")
        else:
            print("  Sitemap не найден или пуст.")
    return urls


# ── Обход по ссылкам ─────────────────────────────────────────────────────────

def crawl(base_url: str, *, max_pages: int = 500, max_depth: int = 3,
          verbose: bool = True) -> list[str]:
    """
    BFS-обход сайта по внутренним ссылкам.
    Возвращает список URL в порядке обнаружения.
    """
    domain = get_domain(base_url)
    seen: set[str] = {base_url}
    queue: deque[tuple[str, int]] = deque([(base_url, 0)])
    collected: list[str] = []

    while queue and len(collected) < max_pages:
        url, depth = queue.popleft()
        collected.append(url)

        if depth >= max_depth:
            continue

        resp = fetch(url)
        if isinstance(resp, Exception):
            continue
        if not is_html_response(resp):
            continue

        soup = parse_html(resp.text)
        for a in soup.find_all("a", href=True):
            link = make_absolute(url, a["href"])
            if not is_same_domain(link, domain):
                continue
            if link in seen:
                continue
            # не лезем в очевидные файлы
            if _is_asset(link):
                continue
            seen.add(link)
            queue.append((link, depth + 1))

    if verbose:
        print(f"  Краулер: собрано {len(collected)} URL (глубина до {max_depth}).")
    return collected


# ── Сбор всех ссылок со страницы (для проверки битых) ────────────────────────

def extract_links(html: str, page_url: str) -> dict[str, list[str]]:
    """
    Возвращает словарь:
        {
            "internal": [...],   # ссылки на тот же домен
            "external": [...],   # ссылки на другие домены
        }
    Учитываются только <a href>, без якорей (#) и javascript:.
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


def extract_images(html: str, page_url: str) -> list[dict]:
    """
    Возвращает список изображений со страницы:
        [{"src": "...", "alt": "...", "has_alt": True/False}, ...]
    """
    soup = parse_html(html)
    images: list[dict] = []

    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        srcset = img.get("srcset", "").strip()
        # берём первый src (или первый кандидат из srcset)
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


# ── Вспомогательные ──────────────────────────────────────────────────────────

_ASSET_EXTENSIONS = frozenset((
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".gz", ".tar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".xml", ".json", ".csv", ".txt", ".rss",
))


def _is_asset(url: str) -> bool:
    """Грубая проверка: URL ведёт на файл-ресурс, а не HTML-страницу."""
    path = url.split("?")[0].lower()
    return any(path.endswith(ext) for ext in _ASSET_EXTENSIONS)
