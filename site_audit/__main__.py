# site_audit/__main__.py
"""
CLI-оркестратор аудита сайта.

Запуск:
    python -m site_audit https://example.com
    python -m site_audit https://example.com --checks seo,empty_pages --limit 50
    python -m site_audit https://example.com --workers 20 --output-dir ./reports
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import fetch, get_domain, is_html_response
from .crawler import try_sitemap, crawl
from .checks import empty_pages, seo, broken_links, images, redirects, duplicates, placeholders
from .report import save_all

ALL_CHECKS: dict[str, dict] = {
    "empty_pages": {
        "module": empty_pages,
        "description": empty_pages.DESCRIPTION,
    },
    "seo": {
        "module": seo,
        "description": seo.DESCRIPTION,
    },
    "broken_links": {
        "module": broken_links,
        "description": broken_links.DESCRIPTION,
    },
    "images": {
        "module": images,
        "description": images.DESCRIPTION,
    },
    "redirects": {
        "module": redirects,
        "description": redirects.DESCRIPTION,
    },
    "duplicates": {
        "module": duplicates,
        "description": duplicates.DESCRIPTION,
    },
    "placeholders": {
        "module": placeholders,
        "description": placeholders.DESCRIPTION,
    },
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="site_audit",
        description="Комплексный аудит сайта: пустые страницы, SEO, битые ссылки, "
                    "картинки, редиректы, дубли, заглушки.",
    )
    p.add_argument("base_url", help="URL сайта, например https://example.com")

    all_names = ",".join(ALL_CHECKS.keys())
    p.add_argument("--checks", default=all_names,
                   help=f"Список проверок через запятую. По умолчанию: {all_names}")
    p.add_argument("--list-checks", action="store_true",
                   help="Показать доступные проверки и выйти")

    p.add_argument("--max-crawl-pages", type=int, default=500)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--limit", type=int, default=None)

    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--timeout", type=int, default=15)

    p.add_argument("--min-text-length", type=int, default=100)
    p.add_argument("--max-image-size-kb", type=int, default=500)
    p.add_argument("--check-external-links", action="store_true", default=False)

    p.add_argument("--output-dir", default=".")
    p.add_argument("--excel-name", default="audit_report.xlsx")
    p.add_argument("--html-name", default="audit_report.html")
    p.add_argument("--quiet", action="store_true")

    return p.parse_args(argv)


def download_pages(urls: list[str], *,
                   workers: int = 10,
                   delay: float = 0.0,
                   timeout: int = 15,
                   verbose: bool = True) -> list[dict]:
    pages: list[dict] = []
    done = 0
    total = len(urls)

    def _download(url: str) -> dict:
        resp = fetch(url, timeout=timeout)
        html = None
        if isinstance(resp, Exception):
            pass
        elif is_html_response(resp) and resp.status_code == 200:
            html = resp.text
        if delay:
            time.sleep(delay)
        return {"url": url, "resp": resp, "html": html}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download, url): url for url in urls}
        for future in as_completed(futures):
            page = future.result()
            pages.append(page)
            done += 1
            if verbose and done % 25 == 0:
                print(f"  Загружено {done}/{total}")

    if verbose:
        ok = sum(1 for p in pages if p["html"] is not None)
        print(f"  Загрузка завершена: {ok}/{total} страниц с HTML.")

    return pages


def run_checks(check_names: list[str],
               base_url: str,
               urls: list[str],
               pages: list[dict],
               args: argparse.Namespace,
               verbose: bool = True) -> tuple[dict[str, list[dict]], dict[str, str]]:
    results: dict[str, list[dict]] = {}
    summaries: dict[str, str] = {}
    domain = get_domain(base_url)

    for name in check_names:
        entry = ALL_CHECKS[name]
        mod = entry["module"]

        if verbose:
            print(f"\n{'='*60}")
            print(f"  Проверка: {entry['description']}")
            print(f"{'='*60}")

        t0 = time.time()

        try:
            if name == "empty_pages":
                res = mod.check_many(pages, min_text_length=args.min_text_length)
                res = mod.filter_empty(res)

            elif name == "seo":
                res = mod.check_many(pages)
                res = mod.filter_with_issues(res)

            elif name == "broken_links":
                res = mod.check(
                    pages,
                    check_external=args.check_external_links,
                    workers=args.workers,
                )

            elif name == "images":
                res = mod.check(
                    pages,
                    max_size_kb=args.max_image_size_kb,
                    workers=args.workers,
                )

            elif name == "redirects":
                res = mod.check(
                    urls,
                    site_domain=domain,
                    workers=args.workers,
                )
                extra = mod.check_internal_links_to_redirects(
                    pages,
                    workers=args.workers,
                    verbose=verbose,
                )
                res.extend(extra)

            elif name == "duplicates":
                res = mod.check(pages, verbose=verbose)

            elif name == "placeholders":
                res = mod.check_many(pages, verbose=verbose)

            else:
                print(f"  [!] Неизвестная проверка: {name}, пропускаю.")
                continue

        except Exception as exc:
            print(f"  [!] Ошибка в проверке {name}: {exc}")
            res = []

        elapsed = time.time() - t0
        results[name] = res
        summaries[name] = mod.summary(res)

        if verbose:
            print(mod.summary(res))
            print(f"  (выполнено за {elapsed:.1f} сек)")

    return results, summaries


def main(argv: list[str] | None = None):
    args = parse_args(argv)

    if args.list_checks:
        print("Доступные проверки:")
        for name, entry in ALL_CHECKS.items():
            print(f"  {name:20s} — {entry['description']}")
        return

    base_url = args.base_url.rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    verbose = not args.quiet
    check_names = [c.strip() for c in args.checks.split(",") if c.strip()]

    for name in check_names:
        if name not in ALL_CHECKS:
            print(f"Неизвестная проверка: {name}")
            print(f"Доступные: {', '.join(ALL_CHECKS.keys())}")
            sys.exit(1)

    # ── 1. Сбор URL ────────────────────────────────────────────────────
    print(f"\n[1/4] Сбор URL с {base_url} ...")
    urls_set = try_sitemap(base_url, verbose=verbose)
    if urls_set:
        urls = list(urls_set)
    else:
        print("  Обхожу сайт по ссылкам...")
        urls = crawl(base_url, max_pages=args.max_crawl_pages,
                     max_depth=args.max_depth, verbose=verbose)

    if args.limit:
        urls = urls[:args.limit]

    print(f"  Итого URL для проверки: {len(urls)}")

    if not urls:
        print("  Не найдено ни одного URL. Проверьте адрес сайта.")
        sys.exit(1)

    # ── 2. Загрузка страниц ────────────────────────────────────────────
    print(f"\n[2/4] Загрузка {len(urls)} страниц ({args.workers} потоков)...")
    pages = download_pages(
        urls,
        workers=args.workers,
        delay=args.delay,
        timeout=args.timeout,
        verbose=verbose,
    )

    # ── 3. Проверки ────────────────────────────────────────────────────
    print(f"\n[3/4] Запуск проверок: {', '.join(check_names)}")
    results, summaries = run_checks(
        check_names, base_url, urls, pages, args, verbose=verbose,
    )

    # ── 4. Отчёты ──────────────────────────────────────────────────────
    print(f"\n[4/4] Генерация отчётов...")
    save_all(
        results, summaries,
        base_url=base_url,
        output_dir=args.output_dir,
        excel_name=args.excel_name,
        html_name=args.html_name,
    )

    # ── Итоговая сводка ────────────────────────────────────────────────
    total = sum(len(rows) for rows in results.values())
    print(f"\n{'='*60}")
    print(f"  АУДИТ ЗАВЕРШЁН")
    print(f"  Сайт:    {base_url}")
    print(f"  Страниц: {len(urls)}")
    print(f"  Проблем:  {total}")
    print(f"{'='*60}")
    for name in check_names:
        count = len(results.get(name, []))
        label = ALL_CHECKS[name]["description"]
        marker = "✓" if count == 0 else "✗"
        print(f"  {marker} {label}: {count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
