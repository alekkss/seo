"""
Сервис аудита сайта.
Инкапсулирует полный цикл: сбор URL → загрузка страниц → проверки → генерация отчётов.
Используется как единая точка входа для CLI и Telegram-бота.
Основной метод — run_audit_async (асинхронный).
Синхронная обёртка run_audit сохранена для обратной совместимости.

Архитектура потоковой обработки:
    Страницы обрабатываются батчами по _STREAM_BATCH_SIZE (2000) URL:
      1. Загружается батч HTML.
      2. Per-page проверки (seo, empty, meta, heading, mixed, placeholders)
         выполняются над батчем, результаты накапливаются.
      3. Из батча извлекаются компактные метаданные для cross-page проверок
         (title, description, h1, hash текста, внутренние ссылки).
      4. HTML батча освобождается из памяти.
      5. После всех батчей запускаются cross-page проверки (duplicates,
         orphan_pages), работающие только с метаданными.
    Это позволяет проверять сайты с 20 000+ страниц на VPS с 2 ГБ RAM.

Архитектура параллельности:
    Уровень параллельности определяется ЕДИНЫМ семафором, значение которого
    вычисляется функцией compute_optimal_semaphore() из utils.py:
      - С прокси: proxy_count * max_connections_per_proxy
      - Без прокси: fallback_max_concurrent (= params.workers)

Использование:
    from site_audit.services import AuditService
    from site_audit.config import get_settings

    settings = get_settings()
    service = AuditService(settings)

    result = await service.run_audit_async(params)
"""
from __future__ import annotations

import asyncio
import gc
import time
from collections import defaultdict
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
    compute_optimal_semaphore,
    create_aiohttp_session,
    get_domain,
    is_async_response_ok,
    normalize_url,
)

logger = get_logger("service.audit")

# ── Классификация проверок ────────────────────────────────────────────────

# Per-page проверки: обрабатывают каждую страницу независимо.
# Запускаются на каждом батче, результаты накапливаются.
_PER_PAGE_CHECKS: frozenset[str] = frozenset({
    "empty_pages",
    "seo",
    "meta_quality",
    "heading_structure",
    "mixed_content",
    "placeholders",
})

# IO-bound per-page проверки: делают свои HTTP-запросы,
# вызываются через await в текущем event loop.
_ASYNC_IO_CHECKS: frozenset[str] = frozenset({
    "broken_links",
    "images",
})

# Cross-page проверки: требуют данные со ВСЕХ страниц для сравнения.
# Запускаются ОДИН РАЗ после обработки всех батчей,
# работают с компактными метаданными, а не с HTML.
_CROSS_PAGE_CHECKS: frozenset[str] = frozenset({
    "duplicates",
    "orphan_pages",
})

# Standalone проверки: не зависят от загруженных страниц.
_STANDALONE_CHECKS: frozenset[str] = frozenset({
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
    workers: int = 20
    delay: float = 0.0

    # Опциональный предохранитель от переполнения памяти.
    # 0 — без лимита (по умолчанию). Установите значение > 0
    # через DEFAULT_MAX_TOTAL_PAGES в .env, если сервер ограничен по памяти.
    max_total_pages: int = 0

    # Сетевые настройки
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


@dataclass
class _CrossPageAccumulator:
    """
    Аккумулятор компактных метаданных для cross-page проверок.

    Хранит только то, что нужно для duplicates и orphan_pages,
    без полного HTML. Для 20 000 страниц занимает ~20 МБ.

    Attributes:
        page_infos: метаданные страниц для duplicates
            (title, description, h1, canonical, text_hash, text_prefix_hash).
        inbound_links: карта входящих ссылок для orphan_pages
            {нормализованный_target_url: {source_url_1, ...}}.
        all_urls: все URL в порядке обнаружения.
    """

    page_infos: list[dict[str, Any]] = field(default_factory=list)
    inbound_links: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    all_urls: list[str] = field(default_factory=list)


class AuditService:
    """
    Сервис выполнения полного цикла аудита сайта.

    Принимает настройки через конструктор (Dependency Injection),
    не зависит от конкретной точки входа (CLI/бот).

    Потоковая обработка:
      Страницы обрабатываются батчами по _STREAM_BATCH_SIZE URL.
      В памяти одновременно находится только HTML одного батча +
      компактный аккумулятор метаданных для cross-page проверок.
      После обработки батча HTML освобождается, GC принудительно
      запускается для возврата памяти ОС.

    Параллельность:
      Определяется единым семафором: proxy_count * max_connections
      или params.workers без прокси.
    """

    # Размер батча потоковой обработки. Определяет пиковое потребление
    # памяти: ~2000 HTML-страниц ≈ 500 МБ–1 ГБ.
    # Для VPS с 2 ГБ RAM — 2000, с 1 ГБ — 1000, с 4 ГБ — 4000.
    _STREAM_BATCH_SIZE: int = 2000

    # Размер порции внутри батча при загрузке через aiohttp.
    # Ограничивает количество одновременно "в полёте" задач.
    _DOWNLOAD_CHUNK_SIZE: int = 500

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._proxy_rotator: ProxyRotator | None = create_proxy_rotator()
        self._sitemap_urls: set[str] | None = None

    @property
    def proxy_rotator(self) -> ProxyRotator | None:
        """Возвращает текущий ротатор прокси (для диагностики)."""
        return self._proxy_rotator

    @staticmethod
    def available_checks() -> dict[str, str]:
        """Возвращает словарь доступных проверок: {имя: описание}."""
        return {name: entry["description"] for name, entry in ALL_CHECKS.items()}

    @staticmethod
    def validate_check_names(names: list[str]) -> list[str]:
        """Возвращает список некорректных имён проверок."""
        return [n for n in names if n not in ALL_CHECKS]

    def create_params_from_settings(self, base_url: str) -> AuditParams:
        """Создаёт AuditParams из настроек .env."""
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
    # Вычисление семафора
    # ═════════════════════════════════════════════════════════════════════

    def _compute_semaphore_value(self, params: AuditParams) -> int:
        """Вычисляет значение общего семафора для HTTP-запросов."""
        return compute_optimal_semaphore(
            proxy_rotator=self._proxy_rotator,
            fallback_max_concurrent=params.workers,
        )

    # ═════════════════════════════════════════════════════════════════════
    # Синхронная обёртка
    # ═════════════════════════════════════════════════════════════════════

    def run_audit(
        self,
        params: AuditParams,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> AuditResult:
        """Синхронная обёртка над run_audit_async."""
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

        Потоковая обработка:
          0. Preflight-проверка прокси
          1. Сбор URL (sitemap / BFS)
          2. Для каждого батча из _STREAM_BATCH_SIZE URL:
             a. Загрузка HTML
             b. Per-page проверки (seo, empty, meta, heading, ...)
             c. IO-bound проверки (broken_links, images)
             d. Извлечение метаданных для cross-page проверок
             e. Освобождение HTML из памяти
          3. Cross-page проверки (duplicates, orphan_pages) по метаданным
          4. Standalone проверки (robots_sitemap)
          5. Генерация отчётов

        Args:
            params: параметры аудита.
            on_progress: callback для прогресса.

        Returns:
            AuditResult с результатами.

        Raises:
            ValueError: если не найдено ни одного URL.
        """
        trace_id = set_trace_id()
        start_time = time.time()

        base_url = params.base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url

        # Логируем старт
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

        # ── 0. Preflight ──────────────────────────────────────────
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

        # ── 2–3. Потоковая обработка батчами ─────────────────────
        semaphore_value = self._compute_semaphore_value(params)
        total_batches = (len(urls) + self._STREAM_BATCH_SIZE - 1) // self._STREAM_BATCH_SIZE

        self._notify(
            on_progress,
            f"⬇️ Этап 2/4: Загрузка и проверка {len(urls)} страниц "
            f"({total_batches} батч(ей) по {self._STREAM_BATCH_SIZE}, "
            f"параллельность: {semaphore_value})...",
        )

        results: dict[str, list[dict[str, Any]]] = {}
        accumulator = _CrossPageAccumulator(all_urls=list(urls))

        # Определяем, какие per-page и IO проверки нужны
        active_per_page = [n for n in params.check_names if n in _PER_PAGE_CHECKS]
        active_io = [n for n in params.check_names if n in _ASYNC_IO_CHECKS]
        active_cross = [n for n in params.check_names if n in _CROSS_PAGE_CHECKS]
        active_standalone = [n for n in params.check_names if n in _STANDALONE_CHECKS]

        # Нужно ли собирать метаданные для cross-page проверок?
        need_dup_meta = "duplicates" in active_cross
        need_orphan_meta = "orphan_pages" in active_cross

        total_ok = 0
        total_done = 0

        for batch_index in range(total_batches):
            batch_start = batch_index * self._STREAM_BATCH_SIZE
            batch_urls = urls[batch_start: batch_start + self._STREAM_BATCH_SIZE]
            batch_num = batch_index + 1

            self._notify(
                on_progress,
                f"⬇️ Батч {batch_num}/{total_batches}: "
                f"загрузка {len(batch_urls)} страниц...",
            )

            # ── 2a. Загрузка HTML батча ───────────────────────────
            pages = await self._download_pages_async(
                batch_urls, params, semaphore_value, on_progress,
            )
            ok_count = sum(1 for p in pages if p["html"] is not None)
            total_ok += ok_count
            total_done += len(batch_urls)

            logger.info(
                "Батч загружен",
                extra={"context": {
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "urls": len(batch_urls),
                    "with_html": ok_count,
                }},
            )

            # ── 2b. Конвертируем для совместимости с проверками ───
            compatible_pages = _make_compatible_pages(pages)

            # ── 2c. Per-page проверки (CPU-bound) ─────────────────
            loop = asyncio.get_running_loop()
            for name in active_per_page:
                entry = ALL_CHECKS[name]
                mod = entry["module"]

                t0 = time.time()
                try:
                    res = await loop.run_in_executor(
                        None,
                        partial(
                            self._execute_sync_check,
                            name, mod, compatible_pages, batch_urls,
                            get_domain(base_url), params, base_url,
                            self._sitemap_urls,
                        ),
                    )
                except Exception as exc:
                    logger.error(
                        "Ошибка в per-page проверке",
                        extra={"context": {
                            "check": name,
                            "batch": batch_num,
                            "error": str(exc),
                        }},
                        exc_info=True,
                    )
                    res = []

                results.setdefault(name, []).extend(res)
                elapsed_check = time.time() - t0
                logger.debug(
                    "Per-page проверка завершена",
                    extra={"context": {
                        "check": name,
                        "batch": batch_num,
                        "issues": len(res),
                        "elapsed": round(elapsed_check, 1),
                    }},
                )

            # ── 2d. IO-bound проверки (broken_links, images) ─────
            for name in active_io:
                entry = ALL_CHECKS[name]
                mod = entry["module"]

                t0 = time.time()
                try:
                    res = await self._execute_async_check(
                        name, mod, compatible_pages, params,
                        semaphore_value, base_url,
                    )
                except Exception as exc:
                    logger.error(
                        "Ошибка в IO-bound проверке",
                        extra={"context": {
                            "check": name,
                            "batch": batch_num,
                            "error": str(exc),
                        }},
                        exc_info=True,
                    )
                    res = []

                results.setdefault(name, []).extend(res)
                elapsed_check = time.time() - t0
                logger.debug(
                    "IO-bound проверка завершена",
                    extra={"context": {
                        "check": name,
                        "batch": batch_num,
                        "issues": len(res),
                        "elapsed": round(elapsed_check, 1),
                    }},
                )

            # ── 2e. Извлечение метаданных для cross-page ─────────
            if need_dup_meta:
                self._extract_dup_metadata(compatible_pages, accumulator)

            if need_orphan_meta:
                self._extract_orphan_metadata(compatible_pages, accumulator)

            # ── 2f. Освобождение HTML ─────────────────────────────
            del pages
            del compatible_pages
            gc.collect()

            self._notify(
                on_progress,
                f"⬇️ Батч {batch_num}/{total_batches} обработан. "
                f"Загружено с HTML: {total_ok}/{total_done}",
            )

        self._notify(
            on_progress,
            f"⬇️ Все страницы обработаны: {total_ok}/{len(urls)} с HTML",
        )

        # ── 3. Cross-page проверки (по метаданным) ────────────────
        if active_cross:
            self._notify(
                on_progress,
                f"🔎 Этап 3/4: Cross-page проверки "
                f"({len(active_cross)} шт.)...",
            )

            loop = asyncio.get_running_loop()
            for name in active_cross:
                entry = ALL_CHECKS[name]
                mod = entry["module"]
                description = entry["description"]

                self._notify(on_progress, f"🔎 {description}...")
                logger.info(
                    "Запуск cross-page проверки",
                    extra={"context": {"check": name}},
                )

                t0 = time.time()
                try:
                    if name == "duplicates":
                        res = await loop.run_in_executor(
                            None,
                            partial(
                                self._run_duplicates_from_metadata,
                                accumulator,
                            ),
                        )
                    elif name == "orphan_pages":
                        res = await loop.run_in_executor(
                            None,
                            partial(
                                self._run_orphan_from_metadata,
                                accumulator, base_url,
                            ),
                        )
                    else:
                        res = []
                except Exception as exc:
                    logger.error(
                        "Ошибка в cross-page проверке",
                        extra={"context": {"check": name, "error": str(exc)}},
                        exc_info=True,
                    )
                    res = []

                results[name] = res
                elapsed_check = time.time() - t0
                logger.info(
                    "Cross-page проверка завершена",
                    extra={"context": {
                        "check": name,
                        "issues": len(res),
                        "elapsed": round(elapsed_check, 1),
                    }},
                )

        # ── 3b. Standalone проверки (robots_sitemap) ─────────────
        if active_standalone:
            for name in active_standalone:
                entry = ALL_CHECKS[name]
                mod = entry["module"]
                description = entry["description"]

                self._notify(on_progress, f"🔎 {description}...")
                logger.info(
                    "Запуск standalone проверки",
                    extra={"context": {"check": name}},
                )

                t0 = time.time()
                try:
                    if name == "robots_sitemap":
                        res = await mod.async_check(
                            [],
                            base_url=base_url,
                            sitemap_urls=self._sitemap_urls,
                            proxy_rotator=self._proxy_rotator,
                            timeout=params.timeout,
                        )
                    else:
                        res = []
                except Exception as exc:
                    logger.error(
                        "Ошибка в standalone проверке",
                        extra={"context": {"check": name, "error": str(exc)}},
                        exc_info=True,
                    )
                    res = []

                results[name] = res
                elapsed_check = time.time() - t0
                logger.info(
                    "Standalone проверка завершена",
                    extra={"context": {
                        "check": name,
                        "issues": len(res),
                        "elapsed": round(elapsed_check, 1),
                    }},
                )

        # Освобождаем аккумулятор
        del accumulator
        gc.collect()

        # ── Сводки ────────────────────────────────────────────────
        summaries: dict[str, str] = {}
        for name in params.check_names:
            if name in ALL_CHECKS and name in results:
                mod = ALL_CHECKS[name]["module"]
                summaries[name] = mod.summary(results[name])

        total_issues = sum(len(rows) for rows in results.values())
        self._notify(
            on_progress,
            f"🔎 Проверки завершены. Проблем: {total_issues}",
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
    # Этап 0: Preflight
    # ═════════════════════════════════════════════════════════════════════

    async def _run_preflight(
        self,
        base_url: str,
        params: AuditParams,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Выполняет preflight-проверку прокси с целевым сайтом."""
        if self._proxy_rotator is None or not self._proxy_rotator.is_enabled:
            return

        self._notify(
            on_progress,
            f"🔌 Проверка прокси с сайтом {base_url}...",
        )

        preflight_timeout = min(params.timeout, 15)
        result: PreflightResult = await self._proxy_rotator.preflight_check(
            base_url,
            timeout=preflight_timeout,
        )

        if result.total == 0:
            return

        if result.all_failed:
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
    # Этап 1: Сбор URL
    # ═════════════════════════════════════════════════════════════════════

    async def _collect_urls_async(
        self,
        base_url: str,
        params: AuditParams,
    ) -> list[str]:
        """Асинхронно собирает URL через sitemap или BFS-обход."""
        urls_set = await async_try_sitemap(
            base_url,
            timeout=params.timeout,
            retries=params.retries,
            proxy_rotator=self._proxy_rotator,
            verbose=False,
        )

        if urls_set:
            self._sitemap_urls = set(urls_set)
            logger.info(
                "URL получены из sitemap",
                extra={"context": {"count": len(urls_set)}},
            )
            urls = list(urls_set)
        else:
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

        if params.max_total_pages > 0 and len(urls) > params.max_total_pages:
            logger.warning(
                "Превышен лимит max_total_pages, список URL обрезан",
                extra={"context": {
                    "found": len(urls),
                    "max_total_pages": params.max_total_pages,
                }},
            )
            urls = urls[: params.max_total_pages]

        return urls

    # ═════════════════════════════════════════════════════════════════════
    # Этап 2: Загрузка страниц батча
    # ═════════════════════════════════════════════════════════════════════

    async def _download_pages_async(
        self,
        urls: list[str],
        params: AuditParams,
        semaphore_value: int,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Асинхронно загружает HTML для одного батча URL."""
        semaphore = asyncio.Semaphore(semaphore_value)
        connector_limit = semaphore_value + 20
        session = create_aiohttp_session(
            max_concurrent=connector_limit,
            timeout_total=params.timeout + 30,
            timeout_connect=min(10, params.timeout),
            limit_per_host=0,
        )

        pages: list[dict[str, Any]] = []
        total = len(urls)
        done = 0

        try:
            for chunk_start in range(0, total, self._DOWNLOAD_CHUNK_SIZE):
                chunk_urls = urls[chunk_start: chunk_start + self._DOWNLOAD_CHUNK_SIZE]

                responses = await async_fetch_many(
                    chunk_urls,
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

                done += len(chunk_urls)
                logger.debug(
                    "Chunk загружен",
                    extra={"context": {"done": done, "total": total}},
                )
        finally:
            await session.close()

        return pages

    # ═════════════════════════════════════════════════════════════════════
    # Извлечение метаданных для cross-page проверок
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_dup_metadata(
        pages: list[dict[str, Any]],
        accumulator: _CrossPageAccumulator,
    ) -> None:
        """
        Извлекает компактные метаданные из батча для проверки duplicates.
        Вызывает duplicates._extract_page_info для каждой страницы
        и сохраняет результат в аккумулятор. HTML не сохраняется.

        Args:
            pages: страницы батча в совместимом формате.
            accumulator: аккумулятор метаданных.
        """
        for page in pages:
            info = duplicates._extract_page_info(
                page["url"],
                resp=page.get("resp"),
                html=page.get("html"),
            )
            if info is not None:
                # Удаляем полный текст — оставляем только хеши и метаданные
                info.pop("text", None)
                accumulator.page_infos.append(info)

    @staticmethod
    def _extract_orphan_metadata(
        pages: list[dict[str, Any]],
        accumulator: _CrossPageAccumulator,
    ) -> None:
        """
        Извлекает внутренние ссылки из батча для проверки orphan_pages.
        Строит карту входящих ссылок: target_url → {source_url, ...}.

        Args:
            pages: страницы батча в совместимом формате.
            accumulator: аккумулятор метаданных.
        """
        from site_audit.crawler import extract_links
        from site_audit.utils import is_html_response as _is_html

        for page in pages:
            url = page["url"]
            html = page.get("html")

            if html is None:
                resp = page.get("resp")
                if resp is None or isinstance(resp, Exception):
                    continue
                if isinstance(resp, AsyncResponse):
                    if not resp.ok or resp.status != 200 or not _is_html(resp):
                        continue
                    html = resp.text
                else:
                    if not _is_html(resp):
                        continue
                    html = resp.text

            try:
                links = extract_links(html, url)
            except Exception:
                continue

            for link in links["internal"]:
                norm = normalize_url(link)
                accumulator.inbound_links[norm].add(url)

    # ═════════════════════════════════════════════════════════════════════
    # Cross-page проверки (работают с метаданными)
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _run_duplicates_from_metadata(
        accumulator: _CrossPageAccumulator,
    ) -> list[dict[str, Any]]:
        """
        Запускает проверку дубликатов на основе собранных метаданных.
        Вместо полного HTML использует уже извлечённые title, description,
        h1, canonical, text_hash, text_prefix_hash.

        Args:
            accumulator: аккумулятор с page_infos.

        Returns:
            Список найденных дубликатов.
        """
        infos = accumulator.page_infos
        if not infos:
            return []

        results: list[dict[str, Any]] = []

        # Дубли title
        dup_title = duplicates._find_duplicates(infos, "title", min_length=3)
        for value, urls in dup_title.items():
            results.append({
                "check": duplicates.CHECK_NAME,
                "dup_type": "title",
                "value": value,
                "urls": urls,
                "count": len(urls),
            })

        # Дубли description
        dup_desc = duplicates._find_duplicates(infos, "description", min_length=10)
        for value, urls in dup_desc.items():
            results.append({
                "check": duplicates.CHECK_NAME,
                "dup_type": "description",
                "value": value,
                "urls": urls,
                "count": len(urls),
            })

        # Дубли H1
        dup_h1 = duplicates._find_duplicates(infos, "h1", min_length=3)
        for value, urls in dup_h1.items():
            results.append({
                "check": duplicates.CHECK_NAME,
                "dup_type": "h1",
                "value": value,
                "urls": urls,
                "count": len(urls),
            })

        # Полные дубли контента
        dup_content = duplicates._find_duplicates(infos, "text_hash", min_length=32)
        for hash_val, urls in dup_content.items():
            results.append({
                "check": duplicates.CHECK_NAME,
                "dup_type": "content",
                "value": f"[hash: {hash_val}]",
                "urls": urls,
                "count": len(urls),
            })

        # Near-дубли
        dup_near = duplicates._find_duplicates(
            infos, "text_prefix_hash", min_length=32,
        )
        for hash_val, urls in dup_near.items():
            if hash_val in dup_content:
                continue
            full_hashes = set()
            for info in infos:
                if info["text_prefix_hash"] == hash_val:
                    full_hashes.add(info["text_hash"])
            if len(full_hashes) == 1 and full_hashes.pop() in dup_content:
                continue
            results.append({
                "check": duplicates.CHECK_NAME,
                "dup_type": "near_content",
                "value": f"[prefix hash: {hash_val}]",
                "urls": urls,
                "count": len(urls),
            })

        # Одинаковый canonical
        canon_groups: dict[str, list[str]] = defaultdict(list)
        for info in infos:
            canon = info.get("canonical", "")
            url = info["url"]
            if canon and normalize_url(canon) != normalize_url(url):
                canon_groups[normalize_url(canon)].append(url)
        for canon_val, urls in canon_groups.items():
            if len(urls) >= 2:
                results.append({
                    "check": duplicates.CHECK_NAME,
                    "dup_type": "canonical",
                    "value": canon_val,
                    "urls": urls,
                    "count": len(urls),
                })

        logger.info(
            "Duplicates из метаданных",
            extra={"context": {
                "total_infos": len(infos),
                "groups_found": len(results),
            }},
        )

        return results

    @staticmethod
    def _run_orphan_from_metadata(
        accumulator: _CrossPageAccumulator,
        base_url: str,
    ) -> list[dict[str, Any]]:
        """
        Находит страницы-сироты на основе собранной карты ссылок.

        Args:
            accumulator: аккумулятор с inbound_links и all_urls.
            base_url: корневой URL сайта.

        Returns:
            Список страниц-сирот.
        """
        inbound = accumulator.inbound_links
        all_urls = accumulator.all_urls

        # Нормализуем все URL
        norm_check_urls: dict[str, str] = {}
        for url in all_urls:
            norm = normalize_url(url)
            if norm not in norm_check_urls:
                norm_check_urls[norm] = url

        # Исключаем главную
        excluded_norms: set[str] = set()
        if base_url:
            excluded_norms.add(normalize_url(base_url))
            excluded_norms.add(normalize_url(base_url.rstrip("/") + "/"))

        results: list[dict[str, Any]] = []
        for norm_url, original_url in norm_check_urls.items():
            if norm_url in excluded_norms:
                continue
            sources = inbound.get(norm_url, set())
            if sources:
                continue

            results.append({
                "check": orphan_pages.CHECK_NAME,
                "url": original_url,
                "inbound_links": 0,
                "in_sitemap": None,
                "message": "Нет входящих внутренних ссылок",
            })

        logger.info(
            "Orphan pages из метаданных",
            extra={"context": {
                "total_urls": len(all_urls),
                "orphans_found": len(results),
            }},
        )

        return results

    # ═════════════════════════════════════════════════════════════════════
    # Per-page и IO-bound проверки
    # ═════════════════════════════════════════════════════════════════════

    async def _execute_async_check(
        self,
        name: str,
        mod: Any,
        pages: list[dict[str, Any]],
        params: AuditParams,
        semaphore_value: int,
        base_url: str,
    ) -> list[dict[str, Any]]:
        """Выполняет IO-bound проверку через await в текущем event loop."""
        max_concurrent = max(semaphore_value, 1)

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
        """Выполняет CPU-bound per-page проверку синхронно в executor."""
        if name == "empty_pages":
            res = mod.check_many(
                pages,
                min_text_length=params.min_text_length,
            )
            return mod.filter_empty(res)

        if name == "seo":
            res = mod.check_many(pages)
            return mod.filter_with_issues(res)

        if name == "placeholders":
            return mod.check_many(pages, verbose=False)

        if name == "mixed_content":
            return mod.check_many(pages, verbose=False)

        if name == "meta_quality":
            return mod.check_many(pages, verbose=False)

        if name == "heading_structure":
            return mod.check_many(pages, verbose=False)

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
        """Генерирует Excel и HTML отчёты."""
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
        """Отправляет сообщение о прогрессе через callback."""
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
