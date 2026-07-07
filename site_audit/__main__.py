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

from .config.logger import setup_logging
from .config.settings import Settings, get_settings
from .services.audit_service import ALL_CHECKS, AuditParams, AuditService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    p = argparse.ArgumentParser(
        prog="site_audit",
        description="Комплексный аудит сайта: пустые страницы, SEO, битые ссылки, "
                    "картинки, редиректы, дубли, заглушки.",
    )
    p.add_argument("base_url", nargs="?", default=None,
                   help="URL сайта, например https://example.com")

    all_names = ",".join(ALL_CHECKS.keys())
    p.add_argument("--checks", default=all_names,
                   help=f"Список проверок через запятую. По умолчанию: {all_names}")
    p.add_argument("--list-checks", action="store_true",
                   help="Показать доступные проверки и выйти")

    p.add_argument("--max-crawl-pages", type=int, default=None)
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)

    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--delay", type=float, default=None)
    p.add_argument("--timeout", type=int, default=None)

    p.add_argument("--min-text-length", type=int, default=None)
    p.add_argument("--max-image-size-kb", type=int, default=None)
    p.add_argument("--check-external-links", action="store_true", default=None)

    p.add_argument("--output-dir", default=None)
    p.add_argument("--excel-name", default=None)
    p.add_argument("--html-name", default=None)
    p.add_argument("--quiet", action="store_true")

    return p.parse_args(argv)


def _build_params(args: argparse.Namespace, settings: Settings) -> AuditParams:
    """
    Создаёт AuditParams, объединяя CLI-аргументы и настройки из .env.

    Приоритет: CLI-аргумент > переменная окружения > значение по умолчанию.
    """
    check_names = [c.strip() for c in args.checks.split(",") if c.strip()]

    return AuditParams(
        base_url=args.base_url,
        check_names=check_names,
        max_crawl_pages=args.max_crawl_pages if args.max_crawl_pages is not None
            else settings.default_max_crawl_pages,
        max_depth=args.max_depth if args.max_depth is not None
            else settings.default_max_depth,
        limit=args.limit if args.limit is not None
            else settings.default_limit,
        workers=args.workers if args.workers is not None
            else settings.default_workers,
        delay=args.delay if args.delay is not None
            else settings.default_delay,
        timeout=args.timeout if args.timeout is not None
            else settings.default_timeout,
        min_text_length=args.min_text_length if args.min_text_length is not None
            else settings.default_min_text_length,
        max_image_size_kb=args.max_image_size_kb if args.max_image_size_kb is not None
            else settings.default_max_image_size_kb,
        check_external_links=args.check_external_links if args.check_external_links is not None
            else settings.default_check_external_links,
        output_dir=args.output_dir if args.output_dir is not None
            else settings.output_dir,
        excel_name=args.excel_name if args.excel_name is not None
            else settings.excel_report_name,
        html_name=args.html_name if args.html_name is not None
            else settings.html_report_name,
    )


def _print_progress(message: str) -> None:
    """Callback для вывода прогресса в консоль."""
    print(f"  {message}")


def main(argv: list[str] | None = None) -> None:
    """Главная функция CLI."""
    args = parse_args(argv)

    # ── Список проверок ───────────────────────────────────────
    if args.list_checks:
        print("Доступные проверки:")
        for name, entry in ALL_CHECKS.items():
            print(f"  {name:20s} — {entry['description']}")
        return

    # ── Проверка обязательного аргумента ──────────────────────
    if not args.base_url:
        print("Ошибка: укажите URL сайта для аудита.")
        print("Использование: python -m site_audit https://example.com")
        sys.exit(1)

    # ── Загрузка конфигурации ─────────────────────────────────
    try:
        settings = get_settings()
    except ValueError as exc:
        print(f"Ошибка конфигурации: {exc}")
        sys.exit(1)

    # ── Настройка логирования ─────────────────────────────────
    setup_logging(
        log_level=settings.log_level,
        log_file_path=settings.log_file_path,
        log_max_bytes=settings.log_max_bytes,
        log_backup_count=settings.log_backup_count,
    )

    # ── Подготовка параметров ─────────────────────────────────
    params = _build_params(args, settings)

    # ── Валидация имён проверок ────────────────────────────────
    invalid = AuditService.validate_check_names(params.check_names)
    if invalid:
        print(f"Неизвестные проверки: {', '.join(invalid)}")
        print(f"Доступные: {', '.join(ALL_CHECKS.keys())}")
        sys.exit(1)

    # ── Запуск аудита ─────────────────────────────────────────
    verbose = not args.quiet
    progress_cb = _print_progress if verbose else None

    service = AuditService(settings)

    print(f"\n{'='*60}")
    print(f"  АУДИТ САЙТА: {params.base_url}")
    print(f"{'='*60}\n")

    try:
        result = service.run_audit(params, on_progress=progress_cb)
    except ValueError as exc:
        print(f"\nОшибка: {exc}")
        sys.exit(1)

    # ── Итоговая сводка ───────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  АУДИТ ЗАВЕРШЁН")
    print(f"  Сайт:      {result.base_url}")
    print(f"  Страниц:   {result.total_urls}")
    print(f"  Проблем:   {result.total_issues}")
    print(f"  Время:     {result.elapsed_seconds} сек")
    print(f"  Excel:     {result.excel_path}")
    print(f"  HTML:      {result.html_path}")
    print(f"{'='*60}")

    for name in params.check_names:
        count = len(result.results.get(name, []))
        label = ALL_CHECKS[name]["description"]
        marker = "✓" if count == 0 else "✗"
        print(f"  {marker} {label}: {count}")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
