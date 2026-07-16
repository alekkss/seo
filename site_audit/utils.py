"""
Общие утилиты: синхронный и асинхронный HTTP-клиенты, хелперы для URL и парсинга.
Синхронные функции (fetch, head) используются для точечных запросов.
Асинхронные функции (async_fetch, async_head) — для массовых операций
(загрузка страниц, проверка ссылок, картинок, редиректов).
Фабрика create_aiohttp_session() создаёт переиспользуемую сессию
с ограничением параллельности через asyncio.Semaphore.
ProxyRotator инжектится в функции запросов для прозрачной ротации прокси.

Архитектура семафоров (ВАЖНО):
    Используется ОДИН уровень семафоров — общий семафор, передаваемый
    в async_fetch/async_fetch_many. Он ограничивает общее количество
    одновременных HTTP-запросов. Вызывающий код (audit_service) вычисляет
    значение семафора на основе количества прокси и max_connections:
        semaphore = proxy_count * max_connections_per_proxy
    Семафоры прокси (ProxyRotator.get_semaphore) НЕ используются в async_fetch,
    чтобы избежать двойной блокировки и deadlock'ов.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
import requests
from bs4 import BeautifulSoup

from .config.logger import get_logger

if TYPE_CHECKING:
    from .proxy import ProxyRotator

logger = get_logger("utils.http")

# ── Общие константы ──────────────────────────────────────────────────────────

HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

TAGS_TO_STRIP: list[str] = [
    "script", "style", "noscript", "nav", "header", "footer", "svg",
]

# ── Таймауты по умолчанию для aiohttp ────────────────────────────────────────

_DEFAULT_AIOHTTP_TIMEOUT_TOTAL: int = 90
_DEFAULT_AIOHTTP_TIMEOUT_CONNECT: int = 10
_DEFAULT_AIOHTTP_TIMEOUT_SOCK_CONNECT: int = 15
_DEFAULT_AIOHTTP_TIMEOUT_SOCK_READ: int = 60

# ── Пул соединений ───────────────────────────────────────────────────────────

_DEFAULT_CONNECTOR_LIMIT: int = 200

# ── Retry-стратегия ──────────────────────────────────────────────────────────

_DEFAULT_RETRIES: int = 3
_DEFAULT_RETRY_DELAY: float = 1.5
_MAX_RETRY_DELAY: float = 10.0

# HTTP-коды, при которых выполняется повторная попытка
_RETRY_STATUS_CODES: frozenset[int] = frozenset({
    408,                    # Request Timeout
    429,                    # Too Many Requests
    500, 502, 503, 504,    # Server Errors
    520, 521, 522, 523, 524, 525, 527,  # Cloudflare
})

# HTTP-коды, при которых прокси получает жёсткий отказ (мгновенный cooldown)
_PROXY_HARD_FAIL_STATUS_CODES: frozenset[int] = frozenset({
    407,  # Proxy Authentication Required
})

# HTTP-коды, при которых прокси получает мягкий отказ (инкремент счётчика)
_PROXY_SOFT_FAIL_STATUS_CODES: frozenset[int] = frozenset({
    502,  # Bad Gateway
    503,  # Service Unavailable
})


# ═════════════════════════════════════════════════════════════════════════════
# Результат асинхронного запроса
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class AsyncResponse:
    """
    Результат асинхронного HTTP-запроса.
    Обёртка над данными ответа, не зависящая от aiohttp.ClientResponse,
    чтобы можно было безопасно работать с результатом после закрытия сессии.

    Attributes:
        url: запрошенный URL.
        status: HTTP-статус ответа (0 при ошибке сети).
        headers: заголовки ответа.
        text: тело ответа как строка (пустая при HEAD-запросе или ошибке).
        content: тело ответа как байты (пустые при HEAD-запросе, ошибке,
            или если вызывающий код явно отказался от байтов через
            keep_raw_content=False в async_fetch — см. этот параметр
            для экономии памяти при массовой загрузке страниц).
        final_url: URL после всех редиректов.
        error: описание ошибки (пустая строка, если запрос успешен).
        ok: True, если статус 200–399 и нет ошибки.
    """

    url: str = ""
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    content: bytes = b""
    final_url: str = ""
    error: str = ""
    ok: bool = False

    @property
    def status_code(self) -> int:
        """Алиас для совместимости с requests.Response."""
        return self.status

    def header(self, name: str, default: str = "") -> str:
        """Получает заголовок без учёта регистра."""
        lower_name = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lower_name:
                return value
        return default


# ═════════════════════════════════════════════════════════════════════════════
# Фабрика aiohttp-сессии
# ═════════════════════════════════════════════════════════════════════════════

def create_aiohttp_session(
    *,
    max_concurrent: int = _DEFAULT_CONNECTOR_LIMIT,
    timeout_total: int = _DEFAULT_AIOHTTP_TIMEOUT_TOTAL,
    timeout_connect: int = _DEFAULT_AIOHTTP_TIMEOUT_CONNECT,
    limit_per_host: int = 0,
) -> aiohttp.ClientSession:
    """
    Создаёт aiohttp.ClientSession с настроенным пулом соединений.

    Пул (TCPConnector) ограничивает количество одновременных TCP-соединений.
    Семафор (передаётся отдельно в async_fetch) — ограничивает количество
    одновременных запросов в рамках бизнес-логики.

    limit_per_host=0 по умолчанию: при использовании прокси запросы идут
    через разные прокси-хосты, и ограничение per-host не имеет смысла.
    Реальное ограничение обеспечивается общим семафором, который вычисляется
    как proxy_count * max_connections_per_proxy.

    trust_env=False — чтобы aiohttp не подхватывал системные прокси,
    прокси передаются явно через параметр proxy в каждом запросе.

    Args:
        max_concurrent: максимум одновременных соединений в пуле.
        timeout_total: общий таймаут запроса в секундах.
        timeout_connect: таймаут установки соединения в секундах.
        limit_per_host: максимум соединений на один хост (0 — без лимита).

    Returns:
        Настроенный объект aiohttp.ClientSession.
    """
    connector = aiohttp.TCPConnector(
        limit=max_concurrent,
        limit_per_host=limit_per_host,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        force_close=False,
    )
    timeout = aiohttp.ClientTimeout(
        total=timeout_total,
        connect=timeout_connect,
        sock_connect=_DEFAULT_AIOHTTP_TIMEOUT_SOCK_CONNECT,
        sock_read=_DEFAULT_AIOHTTP_TIMEOUT_SOCK_READ,
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers=HEADERS,
        trust_env=False,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Вычисление оптимального семафора
# ═════════════════════════════════════════════════════════════════════════════

def compute_optimal_semaphore(
    *,
    proxy_rotator: ProxyRotator | None = None,
    fallback_max_concurrent: int = 20,
) -> int:
    """
    Вычисляет оптимальное значение семафора на основе пула прокси.

    Формула: proxy_count * max_connections_per_proxy.
    Если прокси нет или отключены — используется fallback_max_concurrent.
    Результат ограничен сверху значением 200 (лимит коннектора).

    Это ЕДИНСТВЕННОЕ место, где определяется уровень параллельности
    для HTTP-запросов. Вызывается из audit_service при создании
    семафора для загрузки страниц и для проверок.

    Args:
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        fallback_max_concurrent: параллельность без прокси.

    Returns:
        Значение для asyncio.Semaphore.
    """
    if proxy_rotator is not None and proxy_rotator.is_enabled:
        # Используем все прокси на полную: count * max_connections
        optimal = proxy_rotator.total * proxy_rotator.max_connections
        # Минимум 10, чтобы не деградировать при малом пуле
        optimal = max(optimal, 10)
        # Максимум — лимит коннектора
        optimal = min(optimal, _DEFAULT_CONNECTOR_LIMIT)
        logger.info(
            "Семафор вычислен по пулу прокси",
            extra={"context": {
                "proxy_count": proxy_rotator.total,
                "max_connections_per_proxy": proxy_rotator.max_connections,
                "semaphore_value": optimal,
            }},
        )
        return optimal

    logger.info(
        "Семафор без прокси",
        extra={"context": {"semaphore_value": fallback_max_concurrent}},
    )
    return fallback_max_concurrent


# ═════════════════════════════════════════════════════════════════════════════
# Асинхронный HTTP-клиент
# ═════════════════════════════════════════════════════════════════════════════

def _compute_retry_delay(attempt: int, base_delay: float) -> float:
    """
    Вычисляет задержку перед повторной попыткой.
    Использует экспоненциальный backoff с джиттером,
    чтобы избежать thundering herd при массовых сбоях.

    Args:
        attempt: номер попытки (0-based).
        base_delay: базовая задержка в секундах.

    Returns:
        Задержка в секундах с джиттером, не более _MAX_RETRY_DELAY.
    """
    exponential = base_delay * (2 ** attempt)
    capped = min(exponential, _MAX_RETRY_DELAY)
    jitter = random.uniform(0, capped * 0.3)
    return capped + jitter


def _safe_proxy_label(proxy_url: str | None) -> str:
    """
    Формирует безопасную метку прокси для логов (без пароля).

    Args:
        proxy_url: URL прокси или None.

    Returns:
        Строка вида 'host:port' или 'напрямую'.
    """
    if not proxy_url:
        return "напрямую"
    # URL вида http://login:password@host:port — берём часть после @
    if "@" in proxy_url:
        return proxy_url.split("@")[-1]
    return proxy_url


async def async_fetch(
    url: str,
    *,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore | None = None,
    proxy_rotator: ProxyRotator | None = None,
    method: str = "GET",
    timeout: int | None = None,
    retries: int = _DEFAULT_RETRIES,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
    allow_redirects: bool = True,
    read_body: bool = True,
    keep_raw_content: bool = True,
) -> AsyncResponse:
    """
    Асинхронный HTTP-запрос с повторами, ограничением параллельности
    и поддержкой прокси-ротации с мягкими/жёсткими ошибками.

    Архитектура семафоров:
        Используется ОДИН семафор (semaphore) — общий ограничитель
        параллельности. Семафор захватывается ТОЛЬКО на время HTTP-запроса,
        а НЕ на время retry-ожидания. Это критически важно: при 3 retry
        с backoff один неудачный URL может блокировать слот на 30+ секунд,
        если семафор не освобождать между попытками.

    При каждой попытке запрашивает прокси у ProxyRotator (если передан).
    При жёсткой ошибке (проблема прокси) — мгновенный cooldown.
    При мягкой ошибке (таймаут) — инкремент счётчика.
    При успехе — сброс счётчика.

    Args:
        url: адрес запроса.
        session: переиспользуемая aiohttp-сессия.
        semaphore: ограничитель общей параллельности (None — без ограничения).
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        method: HTTP-метод (GET, HEAD).
        timeout: переопределение таймаута для этого запроса (секунды).
        retries: количество повторных попыток.
        retry_delay: базовая задержка между попытками (секунды).
        allow_redirects: следовать ли за редиректами.
        read_body: читать ли тело ответа (False для HEAD-запросов).
        keep_raw_content: сохранять ли сырые байты тела ответа в
            AsyncResponse.content. По умолчанию True (полная обратная
            совместимость). Установите False, если вызывающему коду
            нужен только .text — это вдвое сокращает память на страницу.

    Returns:
        AsyncResponse с данными ответа.
    """
    # Формируем таймаут для этого запроса
    request_timeout: aiohttp.ClientTimeout | None = None
    if timeout is not None:
        request_timeout = aiohttp.ClientTimeout(
            total=timeout,
            connect=min(_DEFAULT_AIOHTTP_TIMEOUT_CONNECT, timeout),
            sock_connect=min(_DEFAULT_AIOHTTP_TIMEOUT_SOCK_CONNECT, timeout),
            sock_read=min(_DEFAULT_AIOHTTP_TIMEOUT_SOCK_READ, timeout),
        )

    last_error = ""
    last_status = 0

    for attempt in range(retries + 1):
        # Получаем прокси для этой попытки (может быть None)
        proxy_url: str | None = None
        if proxy_rotator is not None and proxy_rotator.is_enabled:
            proxy_url = proxy_rotator.get_next()
            if proxy_url:
                logger.debug(
                    "Запрос через прокси",
                    extra={"context": {
                        "url": url,
                        "proxy": _safe_proxy_label(proxy_url),
                        "attempt": attempt + 1,
                    }},
                )

        try:
            # Семафор захватывается ТОЛЬКО на время HTTP-запроса,
            # а НЕ на время retry-ожидания между попытками
            result = await _do_single_request(
                url=url,
                session=session,
                semaphore=semaphore,
                proxy_url=proxy_url,
                proxy_rotator=proxy_rotator,
                method=method,
                request_timeout=request_timeout,
                allow_redirects=allow_redirects,
                read_body=read_body,
                keep_raw_content=keep_raw_content,
                attempt=attempt,
            )

            if result is not None:
                # Запрос завершён (успешно или с HTTP-ошибкой, но без сетевой)
                return result

        except _RetryableError as err:
            # Сетевая ошибка, которую можно повторить
            last_error = err.message
            last_status = 0

        except _NonRetryableError as err:
            # Ошибка, которую нет смысла повторять (SSL и т.п.)
            last_error = err.message
            last_status = 0
            break

        # ── Ожидание перед повтором (ВНЕ семафора) ──────────────────
        if attempt < retries:
            wait_time = _compute_retry_delay(attempt, retry_delay)
            logger.debug(
                "Повторная попытка",
                extra={"context": {
                    "url": url,
                    "next_attempt": attempt + 2,
                    "wait_seconds": round(wait_time, 2),
                }},
            )
            await asyncio.sleep(wait_time)

    # Все попытки исчерпаны
    if last_error:
        logger.error(
            "Все попытки исчерпаны",
            extra={"context": {
                "url": url,
                "attempts": retries + 1,
                "last_error": last_error,
            }},
        )

    return AsyncResponse(
        url=url,
        status=last_status,
        error=last_error,
        ok=False,
    )


class _RetryableError(Exception):
    """Сетевая ошибка, при которой имеет смысл повторить запрос."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _NonRetryableError(Exception):
    """Ошибка, при которой повтор не поможет (SSL, и т.п.)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


async def _do_single_request(
    *,
    url: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore | None,
    proxy_url: str | None,
    proxy_rotator: ProxyRotator | None,
    method: str,
    request_timeout: aiohttp.ClientTimeout | None,
    allow_redirects: bool,
    read_body: bool,
    keep_raw_content: bool,
    attempt: int,
) -> AsyncResponse | None:
    """
    Выполняет один HTTP-запрос внутри семафора.

    Возвращает AsyncResponse, если запрос завершился (успешно или с HTTP-ошибкой).
    Возвращает None — не используется, вместо этого бросает исключения:
      - _RetryableError — для ошибок, которые можно повторить.
      - _NonRetryableError — для ошибок, которые нет смысла повторять.

    Семафор захватывается только на время реального HTTP-запроса.

    Args:
        url: адрес запроса.
        session: aiohttp-сессия.
        semaphore: общий семафор параллельности.
        proxy_url: URL прокси для этого запроса (или None).
        proxy_rotator: ротатор для mark_success/mark_fail.
        method: HTTP-метод.
        request_timeout: таймаут запроса.
        allow_redirects: следовать за редиректами.
        read_body: читать тело ответа.
        keep_raw_content: сохранять сырые байты.
        attempt: номер попытки (для логов).

    Returns:
        AsyncResponse при завершении запроса.

    Raises:
        _RetryableError: при сетевых ошибках, которые можно повторить.
        _NonRetryableError: при ошибках, которые нет смысла повторять.
    """
    try:
        # ── Захват семафора → HTTP-запрос → освобождение ──────────
        if semaphore is not None:
            await semaphore.acquire()
        try:
            async with session.request(
                method,
                url,
                timeout=request_timeout,
                allow_redirects=allow_redirects,
                proxy=proxy_url,
            ) as resp:
                last_status = resp.status
                final = str(resp.url)
                resp_headers: dict[str, str] = {
                    k: v for k, v in resp.headers.items()
                }

                body_bytes = b""
                body_text = ""
                if read_body and method.upper() != "HEAD":
                    body_bytes = await resp.read()
                    encoding = resp.get_encoding() or "utf-8"
                    try:
                        body_text = body_bytes.decode(
                            encoding, errors="replace",
                        )
                    except (LookupError, UnicodeDecodeError):
                        body_text = body_bytes.decode(
                            "utf-8", errors="replace",
                        )

                is_ok = 200 <= resp.status < 400
        finally:
            if semaphore is not None:
                semaphore.release()

        # ── Обработка результата (уже ВНЕ семафора) ──────────────
        if not is_ok:
            logger.warning(
                "HTTP-ошибка",
                extra={"context": {
                    "url": url,
                    "status": last_status,
                    "attempt": attempt + 1,
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )
            if (
                proxy_url
                and proxy_rotator is not None
                and last_status in _PROXY_HARD_FAIL_STATUS_CODES
            ):
                proxy_rotator.mark_hard_fail(proxy_url)
            elif (
                proxy_url
                and proxy_rotator is not None
                and last_status in _PROXY_SOFT_FAIL_STATUS_CODES
            ):
                proxy_rotator.mark_soft_fail(proxy_url)

            # Retryable HTTP-статусы — бросаем исключение для повтора
            if last_status in _RETRY_STATUS_CODES:
                raise _RetryableError(f"HTTP {last_status}")
        else:
            if proxy_url and proxy_rotator is not None:
                proxy_rotator.mark_success(proxy_url)

        return AsyncResponse(
            url=url,
            status=last_status,
            headers=resp_headers,
            text=body_text,
            content=body_bytes if keep_raw_content else b"",
            final_url=final,
            error="" if is_ok else f"HTTP {last_status}",
            ok=is_ok,
        )

    # ── Обработка сетевых ошибок ─────────────────────────────────────
    except _RetryableError:
        # Пробрасываем дальше — retry обрабатывается в async_fetch
        raise

    except asyncio.TimeoutError:
        logger.warning(
            "Таймаут запроса",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_soft_fail(proxy_url)
        raise _RetryableError(f"Таймаут запроса к {url}")

    except aiohttp.ServerDisconnectedError as exc:
        logger.warning(
            "Сервер разорвал соединение",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_soft_fail(proxy_url)
        raise _RetryableError(f"Сервер разорвал соединение: {url} — {exc}")

    except aiohttp.ClientSSLError as exc:
        logger.error(
            "Ошибка SSL",
            extra={"context": {
                "url": url,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        raise _NonRetryableError(f"Ошибка SSL при подключении к {url}: {exc}")

    except aiohttp.ClientProxyConnectionError as exc:
        logger.warning(
            "Ошибка подключения через прокси",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_hard_fail(proxy_url)
        raise _RetryableError(
            f"Ошибка подключения через прокси к {url}: {exc}",
        )

    except aiohttp.ClientHttpProxyError as exc:
        logger.warning(
            "HTTP-ошибка прокси",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_hard_fail(proxy_url)
        raise _RetryableError(
            f"HTTP-ошибка прокси при запросе к {url}: {exc}",
        )

    except aiohttp.ClientConnectorError as exc:
        logger.warning(
            "Ошибка подключения",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_soft_fail(proxy_url)
        raise _RetryableError(f"Ошибка подключения к {url}: {exc}")

    except aiohttp.ClientPayloadError as exc:
        logger.warning(
            "Ошибка чтения ответа",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_soft_fail(proxy_url)
        raise _RetryableError(f"Ошибка чтения ответа от {url}: {exc}")

    except aiohttp.ClientError as exc:
        logger.warning(
            "Ошибка aiohttp",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        if proxy_url and proxy_rotator is not None:
            proxy_rotator.mark_soft_fail(proxy_url)
        raise _RetryableError(f"{type(exc).__name__}: {exc}")

    except OSError as exc:
        logger.warning(
            "Ошибка сети (OSError)",
            extra={"context": {
                "url": url,
                "attempt": attempt + 1,
                "error": str(exc),
                "proxy": _safe_proxy_label(proxy_url),
            }},
        )
        raise _RetryableError(f"Ошибка сети: {exc}")


async def async_head(
    url: str,
    *,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore | None = None,
    proxy_rotator: ProxyRotator | None = None,
    timeout: int | None = None,
    retries: int = 1,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
    allow_redirects: bool = True,
) -> AsyncResponse:
    """
    Асинхронный HEAD-запрос с поддержкой прокси-ротации.
    Если сервер возвращает 405/501 (HEAD не поддерживается),
    автоматически выполняет GET-запрос.

    Args:
        url: адрес запроса.
        session: переиспользуемая aiohttp-сессия.
        semaphore: ограничитель параллельности.
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        timeout: таймаут запроса в секундах.
        retries: количество повторов.
        retry_delay: задержка между повторами.
        allow_redirects: следовать ли за редиректами.

    Returns:
        AsyncResponse с данными ответа.
    """
    resp = await async_fetch(
        url,
        session=session,
        semaphore=semaphore,
        proxy_rotator=proxy_rotator,
        method="HEAD",
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
        allow_redirects=allow_redirects,
        read_body=False,
    )

    # HEAD не поддерживается — пробуем GET
    if resp.status in (405, 501):
        resp = await async_fetch(
            url,
            session=session,
            semaphore=semaphore,
            proxy_rotator=proxy_rotator,
            method="GET",
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            allow_redirects=allow_redirects,
            read_body=True,
        )

    return resp


async def async_fetch_many(
    urls: list[str],
    *,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    proxy_rotator: ProxyRotator | None = None,
    timeout: int | None = None,
    retries: int = _DEFAULT_RETRIES,
    delay: float = 0.0,
    on_progress: Any | None = None,
    progress_every: int = 50,
    keep_raw_content: bool = True,
) -> list[AsyncResponse]:
    """
    Массовая загрузка URL с ограничением параллельности, прогрессом
    и поддержкой прокси-ротации.

    Задержка (delay) реализована через staggered start: каждая задача
    стартует со смещением delay * index секунд. Это НЕ блокирует семафор
    во время ожидания — задача ждёт ДО захвата семафора, а не после.

    CRITICAL: return_exceptions=True — чтобы один таймаут не ломал весь батч.

    Args:
        urls: список URL для загрузки.
        session: переиспользуемая aiohttp-сессия.
        semaphore: ограничитель параллельности.
        proxy_rotator: ротатор прокси (None — запросы напрямую).
        timeout: таймаут каждого запроса в секундах.
        retries: количество повторов при ошибке.
        delay: задержка между стартами запросов (секунды).
        on_progress: callback(message: str) для отправки прогресса.
        progress_every: как часто вызывать on_progress (каждые N запросов).
        keep_raw_content: передаётся в async_fetch для каждого URL.

    Returns:
        Список AsyncResponse в том же порядке, что и urls.
    """
    total = len(urls)
    done_count = 0
    lock = asyncio.Lock()

    async def _fetch_one(index: int, url: str) -> AsyncResponse:
        nonlocal done_count

        # Staggered start: задержка ДО запроса, но ВНЕ семафора.
        # Запросы стартуют волнами, а не все сразу — это защищает
        # от бана при массовой загрузке, не блокируя семафор.
        if delay > 0 and index > 0:
            await asyncio.sleep(delay * index)

        try:
            resp = await async_fetch(
                url,
                session=session,
                semaphore=semaphore,
                proxy_rotator=proxy_rotator,
                timeout=timeout,
                retries=retries,
                keep_raw_content=keep_raw_content,
            )
        except Exception as exc:
            logger.error(
                "Неожиданная ошибка в async_fetch_many",
                extra={"context": {
                    "url": url,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }},
            )
            resp = AsyncResponse(
                url=url,
                error=f"Неожиданная ошибка: {type(exc).__name__}: {exc}",
                ok=False,
            )

        async with lock:
            done_count += 1
            if on_progress is not None and done_count % progress_every == 0:
                on_progress(f"⬇️ Загружено {done_count}/{total}...")

        return resp

    tasks = [_fetch_one(i, url) for i, url in enumerate(urls)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Оборачиваем любые исключения из gather в AsyncResponse
    final_results: list[AsyncResponse] = []
    for url, result in zip(urls, results):
        if isinstance(result, AsyncResponse):
            final_results.append(result)
        elif isinstance(result, Exception):
            logger.error(
                "Исключение из gather",
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
# Синхронный HTTP-клиент (обратная совместимость)
# ═════════════════════════════════════════════════════════════════════════════

def fetch(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    retry_delay: float = 2.0,
    method: str = "GET",
    allow_redirects: bool = True,
    session: requests.Session | None = None,
    proxy_rotator: ProxyRotator | None = None,
) -> requests.Response | Exception:
    """
    Синхронный GET/HEAD-запрос с повторами при сетевых ошибках и 5xx.
    Поддерживает прокси-ротацию с мягкими/жёсткими ошибками.
    Используется для точечных запросов вне массовых операций.

    Для массовых операций используйте async_fetch / async_fetch_many.

    Args:
        url: адрес запроса.
        timeout: таймаут в секундах.
        retries: количество повторов.
        retry_delay: базовая задержка между повторами.
        method: HTTP-метод (GET, HEAD).
        allow_redirects: следовать ли за редиректами.
        session: переиспользуемая requests.Session.
        proxy_rotator: ротатор прокси (None — запросы напрямую).

    Returns:
        requests.Response при успехе или Exception при исчерпании попыток.
    """
    requester = session or requests
    last: requests.Response | Exception | None = None

    for attempt in range(retries + 1):
        # Получаем прокси для этой попытки
        proxy_url: str | None = None
        proxies_dict: dict[str, str] | None = None
        if proxy_rotator is not None and proxy_rotator.is_enabled:
            proxy_url = proxy_rotator.get_next()
            if proxy_url:
                proxies_dict = {"http": proxy_url, "https": proxy_url}

        try:
            resp = requester.request(
                method, url,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=allow_redirects,
                proxies=proxies_dict,
            )
            last = resp

            if resp.status_code < 500:
                if proxy_url and proxy_rotator is not None:
                    proxy_rotator.mark_success(proxy_url)
                return resp

            logger.warning(
                "HTTP-ошибка (sync)",
                extra={"context": {
                    "url": url,
                    "status": resp.status_code,
                    "attempt": attempt + 1,
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )

            if (
                proxy_url
                and proxy_rotator is not None
                and resp.status_code in _PROXY_HARD_FAIL_STATUS_CODES
            ):
                proxy_rotator.mark_hard_fail(proxy_url)
            elif (
                proxy_url
                and proxy_rotator is not None
                and resp.status_code in _PROXY_SOFT_FAIL_STATUS_CODES
            ):
                proxy_rotator.mark_soft_fail(proxy_url)

        except requests.exceptions.SSLError as exc:
            last = exc
            logger.error(
                "SSL-ошибка (sync)",
                extra={"context": {
                    "url": url,
                    "error": str(exc),
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )
            break

        except requests.exceptions.ProxyError as exc:
            last = exc
            logger.warning(
                "Ошибка прокси (sync)",
                extra={"context": {
                    "url": url,
                    "attempt": attempt + 1,
                    "error": str(exc),
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )
            if proxy_url and proxy_rotator is not None:
                proxy_rotator.mark_hard_fail(proxy_url)

        except requests.exceptions.Timeout as exc:
            last = exc
            logger.warning(
                "Таймаут (sync)",
                extra={"context": {
                    "url": url,
                    "attempt": attempt + 1,
                    "timeout": timeout,
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )
            if proxy_url and proxy_rotator is not None:
                proxy_rotator.mark_soft_fail(proxy_url)

        except requests.exceptions.ConnectionError as exc:
            last = exc
            logger.warning(
                "Ошибка подключения (sync)",
                extra={"context": {
                    "url": url,
                    "attempt": attempt + 1,
                    "error": str(exc),
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )
            if proxy_url and proxy_rotator is not None:
                proxy_rotator.mark_soft_fail(proxy_url)

        except requests.exceptions.RequestException as exc:
            last = exc
            logger.warning(
                "Ошибка requests (sync)",
                extra={"context": {
                    "url": url,
                    "attempt": attempt + 1,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "proxy": _safe_proxy_label(proxy_url),
                }},
            )

        if attempt < retries:
            wait_time = retry_delay * (attempt + 1)
            time.sleep(wait_time)

    if last is not None and isinstance(last, Exception):
        logger.error(
            "Все попытки исчерпаны (sync)",
            extra={"context": {
                "url": url,
                "attempts": retries + 1,
                "error_type": type(last).__name__,
                "error": str(last),
            }},
        )

    return last  # type: ignore[return-value]


def head(url: str, **kwargs: Any) -> requests.Response | Exception:
    """Синхронный HEAD-запрос (для обратной совместимости)."""
    return fetch(url, method="HEAD", **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# URL-хелперы
# ═════════════════════════════════════════════════════════════════════════════

def get_domain(url: str) -> str:
    """Возвращает домен (netloc) из URL."""
    return urlparse(url).netloc


def normalize_url(url: str) -> str:
    """Приводит URL к каноническому виду: убирает фрагмент, trailing slash."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, ""))


def is_same_domain(url: str, domain: str) -> bool:
    """Проверяет, принадлежит ли URL указанному домену."""
    return urlparse(url).netloc == domain


def make_absolute(base: str, href: str) -> str:
    """Превращает относительную ссылку в абсолютную и отрезает фрагмент."""
    return urljoin(base, href).split("#")[0]


# ═════════════════════════════════════════════════════════════════════════════
# Парсинг HTML
# ═════════════════════════════════════════════════════════════════════════════

def parse_html(html: str) -> BeautifulSoup:
    """Парсит HTML-строку в объект BeautifulSoup."""
    return BeautifulSoup(html, "lxml")


def visible_text(
    soup: BeautifulSoup,
    *,
    strip_tags: list[str] | None = None,
) -> str:
    """
    Извлекает видимый текст из <body>, удалив служебные теги.

    Args:
        soup: распарсенный HTML.
        strip_tags: список тегов для удаления (по умолчанию TAGS_TO_STRIP).

    Returns:
        Строка с видимым текстом.
    """
    clone = BeautifulSoup(str(soup), "lxml")
    for tag_name in (strip_tags or TAGS_TO_STRIP):
        for tag in clone.find_all(tag_name):
            tag.decompose()
    body = clone.body
    return body.get_text(separator=" ", strip=True) if body else ""


def text_hash(text: str) -> str:
    """MD5-хеш нормализованного текста (для поиска дублей)."""
    normalized = " ".join(text.lower().split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Хелперы для определения типа ответа
# ═════════════════════════════════════════════════════════════════════════════

def is_html_response(resp: requests.Response | AsyncResponse) -> bool:
    """
    Проверяет, содержит ли ответ HTML.
    Работает и с requests.Response, и с AsyncResponse.
    """
    if isinstance(resp, AsyncResponse):
        ct = resp.header("Content-Type", "")
    else:
        ct = resp.headers.get("Content-Type", "")
    return "text/html" in ct or "application/xhtml+xml" in ct


def is_async_response_ok(resp: AsyncResponse) -> bool:
    """Проверяет, что асинхронный ответ успешен и содержит HTML."""
    return resp.ok and resp.status == 200 and is_html_response(resp)
