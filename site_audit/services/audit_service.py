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
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

from site_audit.checks import (
    broken_links,
    duplicates,
    empty_pages,
    heading_structure,
    images,
    meta_quality,
    mixed_content,
    orphan_pages,
    placeholders,
    robots_sitemap,
    seo,
)
from site_audit.config.logger import get_logger, set_trace_id
from site_audit.config.settings import Settings
from site_audit.crawler import async_crawl, async_try_sitemap, create_proxy_rotator
from site_audit.proxy import PreflightResult, ProxyRotator
from site_audit.report import save_all
from site_audit.utils import (
    AsyncResponse,
    async_fetch_many,
    create_aiohttp_session,
    get_domain,
    is_async_response_ok,
)

logger = get_logger("service.audit")

# ── Проверки, которые выполняют свои HTTP-запросы (IO-bound, async) ────────
# Эти проверки имеют async_check() и должны вызываться напрямую через await
# в текущем event loop, чтобы семафоры прокси работали корректно.
_ASYNC_IO_CHECKS: frozenset[str] = frozenset({
    "broken_links",
    "images",
    "robots_sitemap",
})


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
    "duplicates": {
        "module": duplicates,
        "description": duplicates.DESCRIPTION,
    },
    "placeholders": {
        "module": placeholders,
        "description": placeholders.DESCRIPTION,
    },
    "robots_sitemap": {
        "module": robots_sitemap,
        "description": robots_sitemap.DESCRIPTION,
    },
    "mixed_content": {
        "module": mixed_content,
        "description": mixed_content.DESCRIPTION,
    },
    "orphan_pages": {
        "module": orphan_pages,
        "description": orphan_pages.DESCRIPTION,
    },
    "meta_quality": {
        "module": meta_quality,
        "description": meta_quality.DESCRIPTION,
    },
    "heading_structure": {
        "module": heading_structure,
        "description": heading_structure.DESCRIPTION,
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

    # Жёсткий предохранитель от переполнения памяти: применяется
    # к итоговому списку URL независимо от источника (sitemap или BFS),
    # в отличие от limit (по умолчанию выключен) и max_crawl_pages
    # (действует только на BFS-обход).
    max_total_pages: int = 2000

    # Сетевые настройки (увеличены для устойчивости к медленным серверам)
    timeout: int = 30
    retries: int = 3

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
    При инициализации создаёт ProxyRotator из настроек —
    один ротатор переиспользуется для всех запросов аудита,
    включая проверки битых ссылок, картинок и robots.txt.

    Перед началом аудита выполняет preflight-проверку прокси:
    один GET-запрос к целевому сайту через каждый прокси.
    Нерабочие прокси сразу исключаются из пула. Если ни один
    прокси не работает — аудит продолжается без прокси.

    IO-bound проверки (broken_links, images, robots_sitemap) выполняются
    напрямую через await в текущем event loop, чтобы семафоры прокси
    работали корректно (без конфликтов между разными loop'ами).

    CPU-bound проверки (seo, empty_pages, duplicates, placeholders,
    mixed_content, meta_quality, heading_structure, orphan_pages)
    выполняются в ThreadPoolExecutor через run_in_executor.

    Основной метод — run_audit_async (асинхронный).
    Метод run_audit — синхронная обёртка для удобства вызова.
    """

    # Размер одной порции при загрузке страниц (этап 2). Ограничивает
    # количество одновременно находящихся "в полёте" задач и сетевых
    # буферов aiohttp. НЕ влияет на итоговый объём собранных HTML —
    # это уже ограничено AuditParams.max_total_pages. Задача батчинга —
    # сгладить пиковую нагрузку и дать сборщику мусора Python шанс
    # освободить промежуточные объекты между порциями.
    _DOWNLOAD_BATCH_SIZE: int = 300

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._proxy_rotator: ProxyRotator | None = create_proxy_rotator()
        # Сохраняется после сбора URL для использования в orphan_pages и robots_sitemap
        self._sitemap_urls: set[str] | None = None

    @property
    def proxy_rotator(self) -> ProxyRotator | None:
        """Возвращает текущий ротатор прокси (для диагностики)."""
        return self._proxy_rotator

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
            max_total_pages=s.default_max_total_pages,
            timeout=s.default_timeout,
            retries=s.default_retries,
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
          0. Preflight-проверка прокси с целевым сайтом — async
          1. Сбор URL (sitemap / BFS-обход) — async
          2. Загрузка страниц (aiohttp + семафор) — async
          3. Проверки — IO-bound через await, CPU-bound в executor
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

        # Логируем информацию о прокси при старте аудита
        if self._proxy_rotator is not None and self._proxy_rotator.is_enabled:
            logger.info(
                "Аудит запущен с прокси",
                extra={"context": {
                    "url": base_url,
                    "trace_id": trace_id,
                    "proxy_count": self._proxy_rotator.total,
                    "proxy_healthy": self._proxy_rotator.healthy_count,
                }},
            )
            self._notify(
                on_progress,
                f"🔍 Начинаю аудит: {base_url} "
                f"(прокси: {self._proxy_rotator.total} шт.)",
            )
        else:
            logger.info(
                "Аудит запущен без прокси",
                extra={"context": {"url": base_url, "trace_id": trace_id}},
            )
            self._notify(on_progress, f"🔍 Начинаю аудит: {base_url}")

        # ── 0. Preflight-проверка прокси ──────────────────────────
        await self._run_preflight(base_url, params, on_progress)

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
            extra={"context": {
                "count": len(urls),
                "sitemap_urls": len(self._sitemap_urls) if self._sitemap_urls else 0,
            }},
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
    # Этап 0: Preflight-проверка прокси
    # ═════════════════════════════════════════════════════════════════════

    async def _run_preflight(
        self,
        base_url: str,
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """
        Выполняет preflight-проверку прокси с целевым сайтом.

        Если прокси не настроены или отключены — пропускает.
        Если часть прокси прошла проверку — логирует и продолжает.
        Если ни один прокси не прошёл — отключает прокси и
        продолжает аудит напрямую (graceful degradation).

        Args:
            base_url: URL целевого сайта.
            params: параметры аудита (используется timeout).
            on_progress: callback для прогресса.
        """
        if self._proxy_rotator is None or not self._proxy_rotator.is_enabled:
            return

        self._notify(
            on_progress,
            f"🔌 Проверка прокси с сайтом {base_url}...",
        )

        # Таймаут preflight чуть меньше основного — это быстрая проверка
        preflight_timeout = min(params.timeout, 15)

        result: PreflightResult = await self._proxy_rotator.preflight_check(
            base_url,
            timeout=preflight_timeout,
        )

        if result.total == 0:
            return

        if result.all_failed:
            # Ни один прокси не работает с этим сайтом — отключаем прокси
            self._proxy_rotator = None
            self._notify(
                on_progress,
                f"⚠️ Ни один прокси ({result.total} шт.) не работает "
                f"с {base_url}. Продолжаю без прокси.",
            )
            logger.warning(
                "Preflight: все прокси отклонены, аудит продолжится без прокси",
                extra={"context": {
                    "target_url": base_url,
                    "total_checked": result.total,
                }},
            )
        else:
            self._notify(
                on_progress,
                f"✅ Прокси проверены: {result.passed}/{result.total} работают",
            )
            logger.info(
                "Preflight завершён: рабочие прокси отобраны",
                extra={"context": {
                    "target_url": base_url,
                    "passed": result.passed,
                    "failed": result.failed,
                    "total": result.total,
                }},
            )

    # ═════════════════════════════════════════════════════════════════════
    # Этап 1: Сбор URL (async)
    # ═════════════════════════════════════════════════════════════════════

    async def _collect_urls_async(
        self,
        base_url: str,
        params: AuditParams,
    ) -> list[str]:
        """
        Асинхронно собирает URL через sitemap или BFS-обход.
        Передаёт ProxyRotator для выполнения запросов через прокси.
        Сохраняет полный набор URL из sitemap в self._sitemap_urls
        до применения limit — для использования в orphan_pages и robots_sitemap.

        Применяет два независимых ограничения к итоговому списку:
          1. params.limit — если задан явно (> 0), обрезает список.
          2. params.max_total_pages — жёсткий предохранитель, применяется
             ВСЕГДА, независимо от источника URL и от того, задан ли limit.
             Защищает от переполнения памяти на сайтах с огромным sitemap.

        Args:
            base_url: корневой URL сайта.
            params: параметры аудита.

        Returns:
            Список URL для проверки.
        """
        urls_set = await async_try_sitemap(
            base_url,
            timeout=params.timeout,
            retries=params.retries,
            proxy_rotator=self._proxy_rotator,
            verbose=False,
        )

        if urls_set:
            # Сохраняем полный набор URL из sitemap до применения limit
            self._sitemap_urls = set(urls_set)
            logger.info(
                "URL получены из sitemap",
                extra={"context": {"count": len(urls_set)}},
            )
            urls = list(urls_set)
        else:
            # BFS-обход: sitemap отсутствует
            self._sitemap_urls = None
            logger.info("Sitemap не найден, запускаю BFS-обход")
            urls = await async_crawl(
                base_url,
                max_pages=params.max_crawl_pages,
                max_depth=params.max_depth,
                max_concurrent=params.workers,
                timeout=params.timeout,
                retries=params.retries,
                delay=params.delay,
                proxy_rotator=self._proxy_rotator,
                verbose=False,
            )

        if params.limit > 0:
            urls = urls[: params.limit]

        # Глобальный предохранитель от переполнения памяти: обрезаем
        # список ДО загрузки страниц, если он всё ещё больше допустимого.
        # Срабатывает независимо от того, был ли задан --limit, и
        # независимо от источника (sitemap или BFS).
        if len(urls) > params.max_total_pages:
            logger.warning(
                "Превышен глобальный лимит страниц (max_total_pages), "
                "список URL обрезан для защиты от переполнения памяти",
                extra={"context": {
                    "found": len(urls),
                    "max_total_pages": params.max_total_pages,
                }},
            )
            urls = urls[: params.max_total_pages]

        return urls

    # ═════════════════════════════════════════════════════════════════════
    # Этап 2: Загрузка страниц (async)
    # ═════════════════════════════════════════════════════════════════════

    async def _download_pages_async(
        self,
        urls: list[str],
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Асинхронно загружает HTML всех URL через aiohttp.
        Использует семафор для ограничения количества одновременных запросов.
        Переиспользует одну TCP-сессию для всех запросов.
        Передаёт ProxyRotator для выполнения запросов через прокси.

        Загружает URL порциями по _DOWNLOAD_BATCH_SIZE штук вместо одного
        gather на весь список: это ограничивает пиковое количество
        одновременно "в полёте" задач и сетевых буферов aiohttp, а также
        даёт сборщику мусора Python возможность освободить промежуточные
        объекты предыдущей порции, пока грузится следующая.

        Передаёт keep_raw_content=False в async_fetch_many, так как здесь
        используется только resp.text (см. ниже) — байты тела ответа не
        нужны и не сохраняются, что вдвое сокращает память на страницу.

        Args:
            urls: список URL для загрузки.
            params: параметры аудита (workers, timeout, retries, delay).
            on_progress: callback для прогресса.

        Returns:
            Список словарей {"url", "resp", "html"} для каждого URL.
        """
        max_concurrent = min(params.workers * 5, 100)
        semaphore = asyncio.Semaphore(max_concurrent)
        session = create_aiohttp_session(
            max_concurrent=max_concurrent,
            timeout_total=params.timeout + 30,
            timeout_connect=min(10, params.timeout),
        )

        pages: list[dict[str, Any]] = []
        total = len(urls)
        done = 0

        try:
            for batch_start in range(0, total, self._DOWNLOAD_BATCH_SIZE):
                batch_urls = urls[batch_start: batch_start + self._DOWNLOAD_BATCH_SIZE]

                responses = await async_fetch_many(
                    batch_urls,
                    session=session,
                    semaphore=semaphore,
                    proxy_rotator=self._proxy_rotator,
                    timeout=params.timeout,
                    retries=params.retries,
                    delay=params.delay,
                    keep_raw_content=False,
                )

                for resp in responses:
                    html: str | None = None
                    if is_async_response_ok(resp):
                        html = resp.text
                    pages.append({
                        "url": resp.url,
                        "resp": resp,
                        "html": html,
                    })

                done += len(batch_urls)
                self._notify(on_progress, f"⬇️ Загружено {done}/{total}...")
                logger.debug(
                    "Порция страниц загружена",
                    extra={"context": {"done": done, "total": total}},
                )
        finally:
            await session.close()
            logger.info(
                "Aiohttp-сессия загрузки закрыта",
                extra={"context": {"total_urls": len(urls)}},
            )

        return pages

    # ═════════════════════════════════════════════════════════════════════
    # Этап 3: Проверки
    # ═════════════════════════════════════════════════════════════════════

    async def _run_checks_async(
        self,
        base_url: str,
        urls: list[str],
        pages: list[dict[str, Any]],
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """
        Выполняет выбранные проверки над загруженными страницами.

        Два режима выполнения:
          - IO-bound проверки (broken_links, images, robots_sitemap) —
            вызываются напрямую через await в текущем event loop,
            чтобы семафоры прокси из ProxyRotator работали корректно.
          - CPU-bound проверки (остальные) — запускаются в
            ThreadPoolExecutor через run_in_executor.

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
        compatible_pages = _make_compatible_pages(pages)

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

            self._notify(on_progress, f"🔎 {description}...")

            t0 = time.time()
            try:
                if name in _ASYNC_IO_CHECKS:
                    # IO-bound: вызываем async_check напрямую в текущем loop,
                    # чтобы семафоры прокси работали в одном event loop
                    res = await self._execute_async_check(
                        name, mod, compatible_pages, params,
                        check_workers, base_url,
                    )
                else:
                    # CPU-bound: выносим в executor, чтобы не блокировать loop
                    res = await loop.run_in_executor(
                        None,
                        partial(
                            self._execute_sync_check,
                            name, mod, compatible_pages, urls, domain,
                            params, base_url, self._sitemap_urls,
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

    async def _execute_async_check(
        self,
        name: str,
        mod: Any,
        pages: list[dict[str, Any]],
        params: AuditParams,
        check_workers: int,
        base_url: str,
    ) -> list[dict[str, Any]]:
        """
        Выполняет IO-bound проверку напрямую через await в текущем event loop.
        Это гарантирует, что семафоры прокси из ProxyRotator работают
        в том же event loop, где были созданы — без конфликтов.

        Args:
            name: имя проверки.
            mod: модуль проверки.
            pages: загруженные страницы в совместимом формате.
            params: параметры аудита.
            check_workers: количество одновременных запросов.
            base_url: корневой URL сайта.

        Returns:
            Список найденных проблем.
        """
        max_concurrent = max(check_workers, 1)

        if name == "broken_links":
            return await mod.async_check(
                pages,
                check_external=params.check_external_links,
                max_concurrent=max_concurrent,
                timeout=params.timeout,
                proxy_rotator=self._proxy_rotator,
            )

        if name == "images":
            return await mod.async_check(
                pages,
                max_size_kb=params.max_image_size_kb,
                max_concurrent=max_concurrent,
                timeout=params.timeout,
                proxy_rotator=self._proxy_rotator,
            )

        if name == "robots_sitemap":
            return await mod.async_check(
                pages,
                base_url=base_url,
                sitemap_urls=self._sitemap_urls,
                proxy_rotator=self._proxy_rotator,
                timeout=params.timeout,
            )

        return []

    @staticmethod
    def _execute_sync_check(
        name: str,
        mod: Any,
        pages: list[dict[str, Any]],
        urls: list[str],
        domain: str,
        params: AuditParams,
        base_url: str,
        sitemap_urls: set[str] | None,
    ) -> list[dict[str, Any]]:
        """
        Выполняет CPU-bound проверку синхронно.
        Вызывается из executor (отдельного потока).
        Эти проверки не делают HTTP-запросов — работают только
        с уже загруженным HTML, поэтому не нуждаются в прокси.

        Args:
            name: имя проверки.
            mod: модуль проверки.
            pages: загруженные страницы в совместимом формате.
            urls: список URL.
            domain: домен сайта.
            params: параметры аудита.
            base_url: корневой URL сайта.
            sitemap_urls: множество URL из sitemap (или None).

        Returns:
            Список найденных проблем.
        """
        if name == "empty_pages":
            res = mod.check_many(
                pages,
                min_text_length=params.min_text_length,
            )
            return mod.filter_empty(res)

        if name == "seo":
            res = mod.check_many(pages)
            return mod.filter_with_issues(res)

        if name == "duplicates":
            return mod.check(pages, verbose=False)

        if name == "placeholders":
            return mod.check_many(pages, verbose=False)

        if name == "mixed_content":
            return mod.check_many(pages, verbose=False)

        if name == "meta_quality":
            return mod.check_many(pages, verbose=False)

        if name == "heading_structure":
            return mod.check_many(pages, verbose=False)

        if name == "orphan_pages":
            return mod.check(
                pages,
                all_urls=urls,
                sitemap_urls=sitemap_urls,
                base_url=base_url,
                verbose=False,
            )

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
                new_page["resp"] = ConnectionError(resp.error)
            else:
                new_page["resp"] = resp
        else:
            new_page["resp"] = resp

        compatible.append(new_page)

    return compatible