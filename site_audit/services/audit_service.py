"""
Сервис аудита сайта.

Инкапсулирует полный цикл: сбор URL → загрузка страниц → проверки → генерация отчётов.
Используется как единая точка входа для CLI и Telegram-бота.

Использование:
    from site_audit.services import AuditService
    from site_audit.config import get_settings

    settings = get_settings()
    service = AuditService(settings)
    result = service.run_audit("https://example.com")
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from site_audit.checks import (
    broken_links,
    duplicates,
    empty_pages,
    images,
    placeholders,
    redirects,
    seo,
)
from site_audit.config.logger import get_logger, set_trace_id
from site_audit.config.settings import Settings
from site_audit.crawler import crawl, try_sitemap
from site_audit.report import save_all
from site_audit.utils import fetch, get_domain, is_html_response


logger = get_logger("service.audit")


# ── Реестр проверок ───────────────────────────────────────────

ALL_CHECKS: dict[str, dict[str, Any]] = {
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


@dataclass
class AuditParams:
    """Параметры одного запуска аудита."""

    base_url: str
    check_names: list[str] = field(default_factory=lambda: list(ALL_CHECKS.keys()))
    max_crawl_pages: int = 500
    max_depth: int = 3
    limit: int = 0
    workers: int = 10
    delay: float = 0.0
    timeout: int = 15
    min_text_length: int = 100
    max_image_size_kb: int = 500
    check_external_links: bool = False
    output_dir: str = "./reports"
    excel_name: str = "audit_report.xlsx"
    html_name: str = "audit_report.html"


@dataclass
class AuditResult:
    """Результат завершённого аудита."""

    base_url: str
    total_urls: int
    total_issues: int
    results: dict[str, list[dict[str, Any]]]
    summaries: dict[str, str]
    excel_path: str
    html_path: str
    elapsed_seconds: float


class AuditService:
    """
    Сервис выполнения полного цикла аудита сайта.

    Принимает настройки через конструктор (Dependency Injection),
    не зависит от конкретной точки входа (CLI/бот).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def available_checks() -> dict[str, str]:
        """
        Возвращает словарь доступных проверок: {имя: описание}.

        Returns:
            Словарь с именами и описаниями проверок.
        """
        return {name: entry["description"] for name, entry in ALL_CHECKS.items()}

    @staticmethod
    def validate_check_names(names: list[str]) -> list[str]:
        """
        Проверяет корректность имён проверок.

        Args:
            names: список имён для валидации.

        Returns:
            Список некорректных имён (пустой, если всё верно).
        """
        return [n for n in names if n not in ALL_CHECKS]

    def create_params_from_settings(self, base_url: str) -> AuditParams:
        """
        Создаёт AuditParams из настроек .env с возможностью переопределения URL.

        Args:
            base_url: URL сайта для аудита.

        Returns:
            Объект AuditParams с дефолтными параметрами из конфигурации.
        """
        s = self._settings
        return AuditParams(
            base_url=base_url,
            max_crawl_pages=s.default_max_crawl_pages,
            max_depth=s.default_max_depth,
            limit=s.default_limit,
            workers=s.default_workers,
            delay=s.default_delay,
            timeout=s.default_timeout,
            min_text_length=s.default_min_text_length,
            max_image_size_kb=s.default_max_image_size_kb,
            check_external_links=s.default_check_external_links,
            output_dir=s.output_dir,
            excel_name=s.excel_report_name,
            html_name=s.html_report_name,
        )

    def run_audit(
        self,
        params: AuditParams,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> AuditResult:
        """
        Запускает полный цикл аудита.

        Args:
            params: параметры аудита.
            on_progress: необязательный callback для отправки сообщений о прогрессе
                         (используется Telegram-ботом).

        Returns:
            Объект AuditResult с результатами аудита и путями к отчётам.

        Raises:
            ValueError: если URL пуст или проверки не найдены.
        """
        trace_id = set_trace_id()
        start_time = time.time()

        base_url = params.base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url

        logger.info(
            "Аудит запущен",
            extra={"context": {"url": base_url, "trace_id": trace_id}},
        )
        self._notify(on_progress, f"🔍 Начинаю аудит: {base_url}")

        # ── 1. Сбор URL ──────────────────────────────────────────
        self._notify(on_progress, "📋 Этап 1/4: Сбор URL...")
        urls = self._collect_urls(base_url, params)

        if not urls:
            raise ValueError(
                f"Не найдено ни одного URL на сайте {base_url}. "
                f"Проверьте адрес сайта."
            )

        self._notify(on_progress, f"📋 Найдено URL: {len(urls)}")
        logger.info(
            "URL собраны",
            extra={"context": {"count": len(urls)}},
        )

        # ── 2. Загрузка страниц ──────────────────────────────────
        self._notify(on_progress, f"⬇️ Этап 2/4: Загрузка {len(urls)} страниц...")
        pages = self._download_pages(urls, params, on_progress)

        ok_count = sum(1 for p in pages if p["html"] is not None)
        self._notify(on_progress, f"⬇️ Загружено с HTML: {ok_count}/{len(urls)}")
        logger.info(
            "Страницы загружены",
            extra={"context": {"total": len(urls), "with_html": ok_count}},
        )

        # ── 3. Проверки ──────────────────────────────────────────
        self._notify(on_progress, f"🔎 Этап 3/4: Проверки ({len(params.check_names)} шт.)...")
        results, summaries = self._run_checks(base_url, urls, pages, params, on_progress)

        total_issues = sum(len(rows) for rows in results.values())
        self._notify(on_progress, f"🔎 Проверки завершены. Проблем: {total_issues}")
        logger.info(
            "Проверки завершены",
            extra={"context": {"total_issues": total_issues}},
        )

        # ── 4. Отчёты ────────────────────────────────────────────
        self._notify(on_progress, "📊 Этап 4/4: Генерация отчётов...")
        excel_path, html_path = self._generate_reports(
            results, summaries, base_url, params,
        )

        elapsed = time.time() - start_time
        self._notify(
            on_progress,
            f"✅ Аудит завершён за {elapsed:.1f} сек. Проблем: {total_issues}",
        )
        logger.info(
            "Аудит завершён",
            extra={"context": {
                "elapsed": round(elapsed, 1),
                "total_issues": total_issues,
                "excel": excel_path,
                "html": html_path,
            }},
        )

        return AuditResult(
            base_url=base_url,
            total_urls=len(urls),
            total_issues=total_issues,
            results=results,
            summaries=summaries,
            excel_path=excel_path,
            html_path=html_path,
            elapsed_seconds=round(elapsed, 1),
        )

    # ── Внутренние методы ─────────────────────────────────────

    @staticmethod
    def _notify(callback: Callable[[str], None] | None, message: str) -> None:
        """Отправляет сообщение о прогрессе через callback, если он задан."""
        if callback is not None:
            callback(message)

    @staticmethod
    def _collect_urls(base_url: str, params: AuditParams) -> list[str]:
        """Собирает URL через sitemap или BFS-обход."""
        urls_set = try_sitemap(base_url, verbose=False)
        if urls_set:
            logger.info(
                "URL получены из sitemap",
                extra={"context": {"count": len(urls_set)}},
            )
            urls = list(urls_set)
        else:
            logger.info("Sitemap не найден, запускаю BFS-обход")
            urls = crawl(
                base_url,
                max_pages=params.max_crawl_pages,
                max_depth=params.max_depth,
                verbose=False,
            )

        if params.limit > 0:
            urls = urls[: params.limit]

        return urls

    @staticmethod
    def _download_pages(
        urls: list[str],
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Параллельно скачивает HTML всех URL."""
        pages: list[dict[str, Any]] = []
        done = 0
        total = len(urls)

        def _download_one(url: str) -> dict[str, Any]:
            resp = fetch(url, timeout=params.timeout)
            html = None
            if not isinstance(resp, Exception) and is_html_response(resp) and resp.status_code == 200:
                html = resp.text
            if params.delay > 0:
                time.sleep(params.delay)
            return {"url": url, "resp": resp, "html": html}

        with ThreadPoolExecutor(max_workers=params.workers) as pool:
            futures = {pool.submit(_download_one, url): url for url in urls}
            for future in as_completed(futures):
                try:
                    page = future.result()
                except Exception as exc:
                    url = futures[future]
                    logger.warning(
                        "Ошибка загрузки страницы",
                        extra={"context": {"url": url, "error": str(exc)}},
                    )
                    page = {"url": url, "resp": exc, "html": None}
                pages.append(page)
                done += 1
                if done % 50 == 0:
                    AuditService._notify(
                        on_progress,
                        f"⬇️ Загружено {done}/{total}...",
                    )

        return pages

    @staticmethod
    def _run_checks(
        base_url: str,
        urls: list[str],
        pages: list[dict[str, Any]],
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """Выполняет выбранные проверки над загруженными страницами."""
        results: dict[str, list[dict[str, Any]]] = {}
        summaries: dict[str, str] = {}
        domain = get_domain(base_url)

        for name in params.check_names:
            if name not in ALL_CHECKS:
                logger.warning(
                    "Неизвестная проверка, пропускаю",
                    extra={"context": {"check": name}},
                )
                continue

            entry = ALL_CHECKS[name]
            mod = entry["module"]
            description = entry["description"]

            logger.info(
                "Запуск проверки",
                extra={"context": {"check": name, "description": description}},
            )
            AuditService._notify(on_progress, f"🔎 {description}...")

            t0 = time.time()
            try:
                res = AuditService._execute_single_check(
                    name, mod, pages, urls, domain, params,
                )
            except Exception as exc:
                logger.error(
                    "Ошибка в проверке",
                    extra={"context": {"check": name, "error": str(exc)}},
                    exc_info=True,
                )
                res = []

            elapsed = time.time() - t0
            results[name] = res
            summaries[name] = mod.summary(res)

            logger.info(
                "Проверка завершена",
                extra={"context": {
                    "check": name,
                    "issues": len(res),
                    "elapsed": round(elapsed, 1),
                }},
            )

        return results, summaries

    @staticmethod
    def _execute_single_check(
        name: str,
        mod: Any,
        pages: list[dict[str, Any]],
        urls: list[str],
        domain: str,
        params: AuditParams,
    ) -> list[dict[str, Any]]:
        """Выполняет одну конкретную проверку, возвращает список проблем."""
        if name == "empty_pages":
            res = mod.check_many(pages, min_text_length=params.min_text_length)
            return mod.filter_empty(res)

        if name == "seo":
            res = mod.check_many(pages)
            return mod.filter_with_issues(res)

        if name == "broken_links":
            return mod.check(
                pages,
                check_external=params.check_external_links,
                workers=params.workers,
            )

        if name == "images":
            return mod.check(
                pages,
                max_size_kb=params.max_image_size_kb,
                workers=params.workers,
            )

        if name == "redirects":
            res = mod.check(
                urls,
                site_domain=domain,
                workers=params.workers,
            )
            extra = mod.check_internal_links_to_redirects(
                pages,
                workers=params.workers,
                verbose=False,
            )
            res.extend(extra)
            return res

        if name == "duplicates":
            return mod.check(pages, verbose=False)

        if name == "placeholders":
            return mod.check_many(pages, verbose=False)

        return []

    @staticmethod
    def _generate_reports(
        results: dict[str, list[dict[str, Any]]],
        summaries: dict[str, str],
        base_url: str,
        params: AuditParams,
    ) -> tuple[str, str]:
        """Генерирует Excel и HTML отчёты, возвращает пути к файлам."""
        output_dir = Path(params.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        excel_path = str(output_dir / params.excel_name)
        html_path = str(output_dir / params.html_name)

        save_all(
            results,
            summaries,
            base_url=base_url,
            output_dir=params.output_dir,
            excel_name=params.excel_name,
            html_name=params.html_name,
        )

        logger.info(
            "Отчёты сгенерированы",
            extra={"context": {"excel": excel_path, "html": html_path}},
        )

        return excel_path, html_path
