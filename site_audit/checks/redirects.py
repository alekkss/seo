# site_audit/checks/redirects.py
"""
Проверка: редиректы.

Что проверяется:
  - Цепочки редиректов длиной 2+ (301→301→200)
  - Циклические редиректы (бесконечные петли)
  - Редиректы на внешний домен
  - Внутренние ссылки, ведущие на URL с цепочкой редиректов 2+
  - HTTP → HTTPS редиректы (mixed scheme)
  - Одиночные редиректы (1 хоп) НЕ считаются проблемой
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests

from ..utils import fetch, head, get_domain, normalize_url, is_html_response, HEADERS
from ..crawler import extract_links

CHECK_NAME = "redirects"
DESCRIPTION = "Цепочки редиректов, циклы, HTTP↔HTTPS"

MIN_CHAIN_TO_REPORT = 2


def _trace_redirects(url: str, *, timeout: int = 12, max_hops: int = 10) -> dict:
    result = {
        "url": url,
        "chain": [],
        "final_url": url,
        "final_status": None,
        "is_redirect": False,
        "chain_length": 0,
        "is_loop": False,
        "error": "",
    }

    current = url
    visited: set[str] = set()

    for _ in range(max_hops + 1):
        if current in visited:
            result["is_loop"] = True
            result["error"] = "Циклический редирект"
            break
        visited.add(current)

        try:
            resp = requests.get(
                current,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            result["error"] = str(exc)
            break

        result["chain"].append((current, resp.status_code))
        result["final_status"] = resp.status_code

        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "").strip()
            if not location:
                result["error"] = f"HTTP {resp.status_code} без заголовка Location"
                break
            if not location.startswith("http"):
                location = urljoin(current, location)
            current = location
        else:
            result["final_url"] = current
            break
    else:
        result["error"] = f"Слишком длинная цепочка (>{max_hops} хопов)"

    redirects_in_chain = [c for c in result["chain"] if 300 <= c[1] < 400]
    result["chain_length"] = len(redirects_in_chain)
    result["is_redirect"] = result["chain_length"] > 0

    return result


def _analyze_trace(trace: dict, site_domain: str) -> list[str]:
    issues: list[str] = []

    if trace["is_loop"]:
        issues.append("Циклический редирект")
        return issues

    if trace["error"] and not trace["is_redirect"]:
        issues.append(f"Ошибка: {trace['error']}")
        return issues

    if not trace["is_redirect"]:
        return issues

    if trace["chain_length"] >= MIN_CHAIN_TO_REPORT:
        issues.append(
            f"Длинная цепочка ({trace['chain_length']} хопов): "
            + " → ".join(c[0] for c in trace["chain"])
        )

    final_domain = get_domain(trace["final_url"])
    if final_domain != site_domain:
        issues.append(f"Редирект на внешний домен: {trace['final_url']}")

    orig_scheme = urlparse(trace["url"]).scheme
    final_scheme = urlparse(trace["final_url"]).scheme
    if orig_scheme == "http" and final_scheme == "https":
        issues.append("HTTP → HTTPS редирект (ссылка ведёт на http-версию)")
    elif orig_scheme == "https" and final_scheme == "http":
        issues.append("HTTPS → HTTP редирект (даунгрейд, проблема безопасности)")

    return issues


def check(urls: list[str], *,
          site_domain: str | None = None,
          workers: int = 10,
          timeout: int = 12,
          verbose: bool = True) -> list[dict]:
    if site_domain is None and urls:
        site_domain = get_domain(urls[0])

    if verbose:
        print(f"  [{CHECK_NAME}] Проверяю {len(urls)} URL на редиректы...")

    traces: dict[str, dict] = {}
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_trace_redirects, url, timeout=timeout): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            traces[url] = future.result()
            done += 1
            if verbose and done % 50 == 0:
                print(f"    ...проверено {done}/{len(urls)}")

    results: list[dict] = []
    for url in urls:
        trace = traces[url]
        issues = _analyze_trace(trace, site_domain)
        if not issues:
            continue
        results.append({
            "check": CHECK_NAME,
            "url": url,
            "final_url": trace["final_url"],
            "chain_length": trace["chain_length"],
            "chain": trace["chain"],
            "issues": issues,
        })

    if verbose:
        print(f"  [{CHECK_NAME}] Проблемных: {len(results)}")

    return results


def check_internal_links_to_redirects(pages: list[dict], *,
                                       workers: int = 10,
                                       timeout: int = 12,
                                       verbose: bool = True) -> list[dict]:
    link_map: dict[str, set[str]] = {}

    for page in pages:
        html = page.get("html")
        if html is None:
            resp = page.get("resp")
            if resp is None or isinstance(resp, Exception):
                continue
            if not is_html_response(resp):
                continue
            html = resp.text

        links = extract_links(html, page["url"])
        for link in links["internal"]:
            link_map.setdefault(link, set()).add(page["url"])

    unique_targets = list(link_map.keys())
    if verbose:
        print(
            f"  [{CHECK_NAME}] Проверяю {len(unique_targets)} "
            f"внутренних ссылок на цепочки редиректов..."
        )

    traces: dict[str, dict] = {}
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_trace_redirects, url, timeout=timeout): url
            for url in unique_targets
        }
        for future in as_completed(futures):
            url = futures[future]
            traces[url] = future.result()
            done += 1
            if verbose and done % 100 == 0:
                print(f"    ...проверено {done}/{len(unique_targets)}")

    results: list[dict] = []
    for target, trace in traces.items():
        if not trace["is_redirect"]:
            continue
        if trace["is_loop"]:
            continue
        if trace["chain_length"] < MIN_CHAIN_TO_REPORT:
            continue
        for source in link_map[target]:
            results.append({
                "check": CHECK_NAME,
                "type": "link_to_redirect",
                "source_url": source,
                "linked_url": target,
                "final_url": trace["final_url"],
                "chain_length": trace["chain_length"],
                "issues": [
                    f"Ссылка ведёт на цепочку редиректов ({trace['chain_length']} хопов) → {trace['final_url']}"
                ],
            })

    if verbose:
        print(f"  [{CHECK_NAME}] Ссылок на цепочки редиректов: {len(results)}")

    return results


def summary(results: list[dict]) -> str:
    loops = [r for r in results if any("Циклич" in i for i in r.get("issues", []))]
    chains = [r for r in results if any("Длинная цепочка" in i for i in r.get("issues", []))]
    external = [r for r in results if any("внешний домен" in i for i in r.get("issues", []))]
    scheme = [r for r in results if any("HTTP" in i and "редирект" in i for i in r.get("issues", []))]
    link_redir = [r for r in results if r.get("type") == "link_to_redirect"]

    lines = [
        f"[{CHECK_NAME}] Проблем: {len(results)} "
        f"(циклы: {len(loops)}, длинные цепочки: {len(chains)}, "
        f"на внешние: {len(external)}, HTTP↔HTTPS: {len(scheme)}, "
        f"ссылки на цепочки: {len(link_redir)})"
    ]

    for r in results:
        issues_str = "; ".join(r.get("issues", []))
        url = r.get("url") or r.get("linked_url", "?")
        lines.append(f"  ✗ {url}  —  {issues_str}")

    return "\n".join(lines)