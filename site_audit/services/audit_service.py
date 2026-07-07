"""
Сервис аудита сайта.

Инкапсулирует полный цикл: сбор URL → загрузка страниц → проверки → генерация отчётов.
Используется как единая точка входа для CLI и Telegram-бота.

Основной метод — run_audit_async (асинхронный).
Синхронная обёртка run_audit сохранена для обратной совместимости.

Использование:
    from site_audit.services import AuditService
    from site_audit.config import get_settings

    settings = get_settings()
    service = AuditService(settings)

    # Асинхронный вызов
    result = await service.run_audit_async(params)

    # Синхронный вызов (обёртка над asyncio.run)
    result = service.run_audit(params)
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

import requests as req_lib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
from site_audit.crawler import async_crawl, async_try_sitemap, extract_links
from site_audit.report import save_all
from site_audit.utils import (
    AsyncResponse,
    HEADERS,
    async_fetch_many,
    create_aiohttp_session,
    get_domain,
    is_async_response_ok,
    is_html_response,
)

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


def _create_session(workers: int) -> req_lib.Session:
    """
    Создаёт requests.Session с пулом соединений и retry-политикой.

    Используется для синхронных проверок (broken_links, images, redirects),
    пока они не переведены на async.

    Args:
        workers: количество параллельных потоков (определяет размер пула).

    Returns:
        Настроенный объект Session.
    """
    session = req_lib.Session()
    session.headers.update(HEADERS)

    pool_size = min(workers + 5, 50)
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=Retry(
            total=2,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
        ),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


class AuditService:
    """
    Сервис выполнения полного цикла аудита сайта.

    Принимает настройки через конструктор (Dependency Injection),
    не зависит от конкретной точки входа (CLI/бот).

    Основной метод — run_audit_async (асинхронный).
    Метод run_audit — синхронная обёртка для удобства вызова.
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
        Создаёт AuditParams из настроек .env.

        Args:
            base_url: URL сайта для аудита.

        Returns:
            Объект AuditParams с параметрами из конфигурации.
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

    # ═════════════════════════════════════════════════════════════════════
    # Синхронная обёртка (для CLI и обратной совместимости)
    # ═════════════════════════════════════════════════════════════════════

    def run_audit(
        self,
        params: AuditParams,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> AuditResult:
        """
        Синхронная обёртка над run_audit_async.

        Создаёт новый event loop и запускает асинхронный аудит.
        Используется из CLI и других синхронных контекстов.

        Args:
            params: параметры аудита.
            on_progress: callback для прогресса.

        Returns:
            Объект AuditResult.

        Raises:
            ValueError: если URL пуст или проверки не найдены.
        """
        return asyncio.run(self.run_audit_async(params, on_progress=on_progress))

    # ═════════════════════════════════════════════════════════════════════
    # Асинхронный аудит (основной метод)
    # ═════════════════════════════════════════════════════════════════════

    async def run_audit_async(
        self,
        params: AuditParams,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> AuditResult:
        """
        Запускает полный цикл аудита асинхронно.

        Этапы:
          1. Сбор URL (sitemap / BFS-обход) — async
          2. Загрузка страниц (aiohttp + семафор) — async
          3. Проверки — CPU-bound в executor, IO-bound синхронно (пока)
          4. Генерация отчётов — в executor (IO-bound файловые операции)

        Args:
            params: параметры аудита.
            on_progress: callback для отправки сообщений о прогрессе.

        Returns:
            Объект AuditResult с результатами аудита и путями к отчётам.

        Raises:
            ValueError: если URL пуст или не найдено ни одного URL.
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
        urls = await self._collect_urls_async(base_url, params)

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
        pages = await self._download_pages_async(urls, params, on_progress)

        ok_count = sum(1 for p in pages if p["html"] is not None)
        self._notify(on_progress, f"⬇️ Загружено с HTML: {ok_count}/{len(urls)}")
        logger.info(
            "Страницы загружены",
            extra={"context": {"total": len(urls), "with_html": ok_count}},
        )

        # ── 3. Проверки ──────────────────────────────────────────
        self._notify(
            on_progress,
            f"🔎 Этап 3/4: Проверки ({len(params.check_names)} шт.)...",
        )
        results, summaries = await self._run_checks_async(
            base_url, urls, pages, params, on_progress,
        )

        total_issues = sum(len(rows) for rows in results.values())
        self._notify(on_progress, f"🔎 Проверки завершены. Проблем: {total_issues}")
        logger.info(
            "Проверки завершены",
            extra={"context": {"total_issues": total_issues}},
        )

        # ── 4. Отчёты ────────────────────────────────────────────
        self._notify(on_progress, "📊 Этап 4/4: Генерация отчётов...")

        loop = asyncio.get_running_loop()
        excel_path, html_path = await loop.run_in_executor(
            None,
            partial(
                self._generate_reports,
                results, summaries, base_url, params,
            ),
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

    # ═════════════════════════════════════════════════════════════════════
    # Этап 1: Сбор URL (async)
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _collect_urls_async(
        base_url: str,
        params: AuditParams,
    ) -> list[str]:
        """
        Асинхронно собирает URL через sitemap или BFS-обход.

        Args:
            base_url: корневой URL сайта.
            params: параметры аудита.

        Returns:
            Список URL для проверки.
        """
        urls_set = await async_try_sitemap(base_url, timeout=params.timeout, verbose=False)

        if urls_set:
            logger.info(
                "URL получены из sitemap",
                extra={"context": {"count": len(urls_set)}},
            )
            urls = list(urls_set)
        else:
            logger.info("Sitemap не найден, запускаю BFS-обход")
            urls = await async_crawl(
                base_url,
                max_pages=params.max_crawl_pages,
                max_depth=params.max_depth,
                max_concurrent=params.workers,
                timeout=params.timeout,
                delay=params.delay,
                verbose=False,
            )

        if params.limit > 0:
            urls = urls[: params.limit]

        return urls

    # ═════════════════════════════════════════════════════════════════════
    # Этап 2: Загрузка страниц (async)
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _download_pages_async(
        urls: list[str],
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Асинхронно загружает HTML всех URL через aiohttp.

        Использует семафор для ограничения количества одновременных запросов.
        Переиспользует одну TCP-сессию для всех запросов.

        Args:
            urls: список URL для загрузки.
            params: параметры аудита (workers, timeout, delay).
            on_progress: callback для прогресса.

        Returns:
            Список словарей {"url", "resp", "html"} для каждого URL.
        """
        max_concurrent = min(params.workers * 5, 100)
        semaphore = asyncio.Semaphore(max_concurrent)

        session = create_aiohttp_session(
            max_concurrent=max_concurrent,
            timeout_total=params.timeout + 10,
            timeout_connect=min(params.timeout, 10),
        )

        try:
            responses = await async_fetch_many(
                urls,
                session=session,
                semaphore=semaphore,
                timeout=params.timeout,
                retries=2,
                delay=params.delay,
                on_progress=on_progress,
                progress_every=150,
            )
        finally:
            await session.close()
            logger.info(
                "Aiohttp-сессия загрузки закрыта",
                extra={"context": {"total_urls": len(urls)}},
            )

        pages: list[dict[str, Any]] = []
        for resp in responses:
            html: str | None = None
            if is_async_response_ok(resp):
                html = resp.text

            pages.append({
                "url": resp.url,
                "resp": resp,
                "html": html,
            })

        return pages

    # ═════════════════════════════════════════════════════════════════════
    # Этап 3: Проверки
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _run_checks_async(
        base_url: str,
        urls: list[str],
        pages: list[dict[str, Any]],
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """
        Выполняет выбранные проверки над загруженными страницами.

        CPU-bound проверки (seo, empty_pages, duplicates, placeholders)
        запускаются в ThreadPoolExecutor через run_in_executor,
        чтобы не блокировать event loop.

        IO-bound проверки (broken_links, images, redirects) пока остаются
        синхронными и тоже выполняются в executor. Они будут переведены
        на async в последующих шагах.

        Args:
            base_url: корневой URL сайта.
            urls: список проверяемых URL.
            pages: загруженные страницы.
            params: параметры аудита.
            on_progress: callback для прогресса.

        Returns:
            Кортеж (results, summaries).
        """
        results: dict[str, list[dict[str, Any]]] = {}
        summaries: dict[str, str] = {}
        domain = get_domain(base_url)
        check_workers = min(params.workers, 5)
        loop = asyncio.get_running_loop()

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
                # Выполняем проверку в executor, чтобы не блокировать event loop
                res = await loop.run_in_executor(
                    None,
                    partial(
                        AuditService._execute_single_check,
                        name, mod, pages, urls, domain, params, check_workers,
                    ),
                )
            except Exception as exc:
                logger.error(
                    "Ошибка в проверке",
                    extra={"context": {"check": name, "error": str(exc)}},
                    exc_info=True,
                )
                res = []

            elapsed_check = time.time() - t0
            results[name] = res
            summaries[name] = mod.summary(res)

            logger.info(
                "Проверка завершена",
                extra={"context": {
                    "check": name,
                    "issues": len(res),
                    "elapsed": round(elapsed_check, 1),
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
        check_workers: int,
    ) -> list[dict[str, Any]]:
        """
        Выполняет одну конкретную проверку, возвращает список проблем.

        Вызывается из executor (отдельного потока).

        Args:
            name: имя проверки.
            mod: модуль проверки.
            pages: загруженные страницы.
            urls: список URL.
            domain: домен сайта.
            params: параметры аудита.
            check_workers: количество потоков для IO-bound проверок.

        Returns:
            Список найденных проблем.
        """
        # Конвертируем AsyncResponse в формат, совместимый с проверками
        compatible_pages = _make_compatible_pages(pages)

        if name == "empty_pages":
            res = mod.check_many(
                compatible_pages,
                min_text_length=params.min_text_length,
            )
            return mod.filter_empty(res)

        if name == "seo":
            res = mod.check_many(compatible_pages)
            return mod.filter_with_issues(res)

        if name == "broken_links":
            return mod.check(
                compatible_pages,
                check_external=params.check_external_links,
                workers=check_workers,
            )

        if name == "images":
            return mod.check(
                compatible_pages,
                max_size_kb=params.max_image_size_kb,
                workers=check_workers,
            )

        if name == "redirects":
            res = mod.check(
                urls,
                site_domain=domain,
                workers=check_workers,
            )
            extra = mod.check_internal_links_to_redirects(
                compatible_pages,
                workers=check_workers,
                verbose=False,
            )
            res.extend(extra)
            return res

        if name == "duplicates":
            return mod.check(compatible_pages, verbose=False)

        if name == "placeholders":
            return mod.check_many(compatible_pages, verbose=False)

        return []

    # ═════════════════════════════════════════════════════════════════════
    # Этап 4: Генерация отчётов
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _generate_reports(
        results: dict[str, list[dict[str, Any]]],
        summaries: dict[str, str],
        base_url: str,
        params: AuditParams,
    ) -> tuple[str, str]:
        """
        Генерирует Excel и HTML отчёты, возвращает пути к файлам.

        Args:
            results: результаты проверок.
            summaries: текстовые сводки проверок.
            base_url: URL сайта.
            params: параметры аудита.

        Returns:
            Кортеж (excel_path, html_path).
        """
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

    # ═════════════════════════════════════════════════════════════════════
    # Утилиты
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _notify(callback: Callable[[str], None] | None, message: str) -> None:
        """Отправляет сообщение о прогрессе через callback, если он задан."""
        if callback is not None:
            callback(message)


# ═════════════════════════════════════════════════════════════════════════════
# Конвертация AsyncResponse → формат, совместимый с проверками
# ═════════════════════════════════════════════════════════════════════════════

def _make_compatible_pages(
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Конвертирует страницы с AsyncResponse в формат,
    совместимый с существующими модулями проверок.

    Проверки ожидают:
      - page["html"] — строка HTML или None
      - page["resp"] — объект с атрибутами status_code, headers, text, url
        (или None / Exception)

    AsyncResponse уже имеет status_code (через property), headers (dict),
    text и url — поэтому он совместим с проверками, которые используют
    resp.status_code, resp.headers.get(...), resp.text, resp.url.

    Для проверок, которые проверяют isinstance(resp, Exception),
    создаём Exception из resp.error при наличии ошибки.

    Args:
        pages: список страниц с AsyncResponse.

    Returns:
        Список страниц в совместимом формате.
    """
    compatible: list[dict[str, Any]] = []

    for page in pages:
        resp = page.get("resp")
        new_page: dict[str, Any] = {
            "url": page["url"],
            "html": page.get("html"),
        }

        if isinstance(resp, AsyncResponse):
            if resp.error and resp.status == 0:
                # Сетевая ошибка — проверки ожидают Exception
                new_page["resp"] = ConnectionError(resp.error)
            else:
                # AsyncResponse совместим по интерфейсу с проверками
                new_page["resp"] = resp
        else:
            new_page["resp"] = resp

        compatible.append(new_page)

    return compatible
