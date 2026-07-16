"""
Управление пулом прокси-серверов: загрузка из файла, round-robin ротация,
мягкий и жёсткий отказ, автоматическое восстановление через cooldown.

Архитектура параллельности (ВАЖНО):
    Ограничение одновременных соединений на прокси обеспечивается ЕДИНЫМ
    общим семафором в вызывающем коде (audit_service / utils), а НЕ
    отдельными семафорами на каждый прокси. Значение общего семафора
    вычисляется как proxy_count * max_connections (см. compute_optimal_semaphore
    в utils.py). Это исключает двойную блокировку и deadlock'и.

Два типа ошибок:
  - mark_hard_fail() — проблема прокси (407, ClientProxyConnectionError):
    прокси сразу уходит в cooldown.
  - mark_soft_fail() — возможно проблема сайта (таймаут, 502/503):
    увеличивается счётчик ошибок, cooldown только после max_fails подряд.

Preflight-проверка:
  - preflight_check(target_url) — перед аудитом проверяет каждый прокси
    одним GET-запросом к целевому сайту. Нерабочие прокси сразу уходят
    в cooldown, в пуле остаются только проверенные.

Использование:
    rotator = ProxyRotator.from_file("proxies.txt", cooldown=120, max_fails=3)
    proxy_url = rotator.get_next()       # "http://login:password@host:port" или None
    rotator.mark_soft_fail(proxy_url)    # мягкая ошибка (таймаут)
    rotator.mark_hard_fail(proxy_url)    # жёсткая ошибка (прокси мёртв)
    rotator.mark_success(proxy_url)      # успех — сброс счётчика

    # Preflight-проверка перед аудитом
    result = await rotator.preflight_check("https://example.com", timeout=15)
    if result.passed == 0:
        print("Ни один прокси не работает с этим сайтом!")
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config.logger import get_logger

logger = get_logger("proxy")


@dataclass
class ProxyEntry:
    """
    Один прокси-сервер из пула.

    Attributes:
        host: IP-адрес или доменное имя прокси.
        port: порт подключения.
        login: имя пользователя для аутентификации.
        password: пароль для аутентификации.
        fail_count: счётчик последовательных ошибок (сбрасывается при успехе).
        failed_at: время входа в cooldown (0.0 — прокси активен).
    """

    host: str
    port: int
    login: str
    password: str
    fail_count: int = 0
    failed_at: float = 0.0

    @property
    def url(self) -> str:
        """Формирует URL прокси для aiohttp/requests: http://login:password@host:port."""
        return f"http://{self.login}:{self.password}@{self.host}:{self.port}"

    @property
    def is_in_cooldown(self) -> bool:
        """Прокси находится в cooldown (временно исключён из ротации)."""
        return self.failed_at > 0.0

    def __str__(self) -> str:
        """Строковое представление без пароля (для логов)."""
        return f"{self.host}:{self.port}"


@dataclass
class PreflightResult:
    """
    Результат preflight-проверки прокси перед аудитом.

    Attributes:
        total: общее количество прокси в пуле.
        passed: количество прокси, успешно подключившихся к целевому сайту.
        failed: количество прокси, не прошедших проверку.
        details: детали по каждому прокси (адрес, статус, ошибка).
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    details: list[dict[str, str | bool]] = field(default_factory=list)

    @property
    def all_failed(self) -> bool:
        """Все прокси не прошли проверку."""
        return self.total > 0 and self.passed == 0


class ProxyRotator:
    """
    Round-robin ротатор прокси с мягким/жёстким отказом.

    Принцип работы:
    - Прокси выдаются по кругу (round-robin) через потокобезопасный индекс.
    - mark_soft_fail() увеличивает счётчик ошибок; cooldown наступает
      только после max_fails последовательных ошибок.
    - mark_hard_fail() сразу отправляет прокси в cooldown (ошибка прокси).
    - mark_success() сбрасывает счётчик ошибок.
    - По истечении cooldown прокси автоматически возвращается в пул
      с обнулённым счётчиком.
    - Если все прокси в cooldown — возвращает None (запрос пойдёт напрямую).
    - preflight_check() позволяет заранее отсеять нерабочие прокси.

    Ограничение параллельности:
    - Общее количество одновременных соединений ограничивается ЕДИНЫМ
      семафором в вызывающем коде, а НЕ отдельными семафорами на каждый
      прокси. Значение семафора = proxy_count * max_connections.
    - Свойство max_connections предоставляет значение для вычисления
      семафора (см. compute_optimal_semaphore в utils.py).

    Потокобезопасность обеспечена через threading.Lock, что корректно
    работает и в asyncio (Lock захватывается на микросекунды без I/O).

    Быстрый поиск прокси по URL обеспечивается словарём _by_url для O(1)
    доступа в mark_success/mark_soft_fail/mark_hard_fail.
    """

    def __init__(
        self,
        proxies: list[ProxyEntry],
        *,
        cooldown: float = 120.0,
        max_fails: int = 3,
        max_connections: int = 3,
    ) -> None:
        """
        Инициализирует ротатор с готовым списком прокси.

        Args:
            proxies: список прокси-серверов.
            cooldown: время в секундах, на которое нерабочий прокси
                      исключается из ротации.
            max_fails: количество последовательных мягких ошибок до cooldown.
            max_connections: максимум одновременных соединений на один прокси.
                Используется для вычисления общего семафора в вызывающем
                коде: semaphore = proxy_count * max_connections.
        """
        self._proxies: list[ProxyEntry] = proxies
        self._cooldown: float = cooldown
        self._max_fails: int = max_fails
        self._max_connections: int = max_connections
        self._index: int = 0
        self._lock: threading.Lock = threading.Lock()

        # O(1) поиск прокси по URL — вместо линейного перебора в mark_*
        self._by_url: dict[str, ProxyEntry] = {
            proxy.url: proxy for proxy in proxies
        }

        if self._proxies:
            logger.info(
                "Пул прокси инициализирован",
                extra={"context": {
                    "total_proxies": len(self._proxies),
                    "cooldown_seconds": self._cooldown,
                    "max_fails": self._max_fails,
                    "max_connections_per_proxy": self._max_connections,
                }},
            )
        else:
            logger.info(
                "Прокси не настроены, запросы будут выполняться напрямую",
            )

    @classmethod
    def from_file(
        cls,
        file_path: str,
        *,
        cooldown: float = 120.0,
        max_fails: int = 3,
        max_connections: int = 3,
    ) -> ProxyRotator:
        """
        Фабричный метод: создаёт ротатор, загружая прокси из текстового файла.
        Формат строки: host:port:login:password
        Пустые строки и строки, начинающиеся с #, игнорируются.

        Args:
            file_path: путь к файлу со списком прокси.
            cooldown: время исключения нерабочего прокси (секунды).
            max_fails: порог мягких ошибок до cooldown.
            max_connections: лимит соединений на прокси.

        Returns:
            Экземпляр ProxyRotator с загруженными прокси.
        """
        path = Path(file_path)

        if not path.exists():
            logger.warning(
                "Файл прокси не найден, работаем без прокси",
                extra={"context": {"path": str(path.resolve())}},
            )
            return cls(
                [],
                cooldown=cooldown,
                max_fails=max_fails,
                max_connections=max_connections,
            )

        proxies: list[ProxyEntry] = []
        errors: list[str] = []

        with open(path, encoding="utf-8") as f:
            for line_number, raw_line in enumerate(f, start=1):
                line = raw_line.strip()

                # Пропускаем пустые строки и комментарии
                if not line or line.startswith("#"):
                    continue

                parts = line.split(":")
                if len(parts) != 4:
                    errors.append(
                        f"  Строка {line_number}: ожидается формат "
                        f"host:port:login:password, получено {len(parts)} частей"
                    )
                    continue

                host, port_str, login, password = parts

                # Валидация порта
                try:
                    port = int(port_str)
                except ValueError:
                    errors.append(
                        f"  Строка {line_number}: некорректный порт '{port_str}'"
                    )
                    continue

                if not (1 <= port <= 65535):
                    errors.append(
                        f"  Строка {line_number}: порт {port} вне диапазона 1–65535"
                    )
                    continue

                # Валидация обязательных полей
                if not host:
                    errors.append(f"  Строка {line_number}: пустой host")
                    continue
                if not login or not password:
                    errors.append(f"  Строка {line_number}: пустой login или password")
                    continue

                proxies.append(ProxyEntry(
                    host=host,
                    port=port,
                    login=login,
                    password=password,
                ))

        # Логируем ошибки парсинга
        if errors:
            logger.warning(
                "Ошибки при чтении файла прокси",
                extra={"context": {
                    "path": str(path.resolve()),
                    "errors": errors,
                }},
            )

        logger.info(
            "Прокси загружены из файла",
            extra={"context": {
                "path": str(path.resolve()),
                "loaded": len(proxies),
                "skipped_errors": len(errors),
            }},
        )

        return cls(
            proxies,
            cooldown=cooldown,
            max_fails=max_fails,
            max_connections=max_connections,
        )

    @property
    def is_enabled(self) -> bool:
        """Возвращает True, если в пуле есть хотя бы один прокси."""
        return len(self._proxies) > 0

    @property
    def total(self) -> int:
        """Общее количество прокси в пуле."""
        return len(self._proxies)

    @property
    def max_connections(self) -> int:
        """Максимум одновременных соединений на один прокси."""
        return self._max_connections

    @property
    def healthy_count(self) -> int:
        """Количество прокси, доступных для запросов (не в cooldown)."""
        now = time.monotonic()
        with self._lock:
            return sum(
                1 for p in self._proxies
                if not p.is_in_cooldown or (now - p.failed_at) >= self._cooldown
            )

    def get_next(self) -> str | None:
        """
        Возвращает URL следующего доступного прокси (round-robin).
        Прокси, у которого истёк cooldown, автоматически восстанавливается
        с обнулённым счётчиком ошибок.
        Если все прокси в cooldown — возвращает None (запрос пойдёт напрямую).

        Returns:
            URL прокси в формате http://login:password@host:port или None.
        """
        if not self._proxies:
            return None

        now = time.monotonic()

        with self._lock:
            # Полный обход пула — ищем первый доступный прокси
            for _ in range(len(self._proxies)):
                proxy = self._proxies[self._index % len(self._proxies)]
                self._index += 1

                # Прокси активен (не в cooldown) — отдаём
                if not proxy.is_in_cooldown:
                    return proxy.url

                # Cooldown истёк — восстанавливаем и отдаём
                if (now - proxy.failed_at) >= self._cooldown:
                    proxy.failed_at = 0.0
                    proxy.fail_count = 0
                    logger.info(
                        "Прокси восстановлен после cooldown",
                        extra={"context": {"proxy": str(proxy)}},
                    )
                    return proxy.url

                # Прокси в cooldown — пропускаем

        # Все прокси в cooldown
        logger.warning(
            "Все прокси недоступны, запрос пойдёт напрямую",
            extra={"context": {
                "total_proxies": len(self._proxies),
                "cooldown_seconds": self._cooldown,
            }},
        )
        return None

    def _find_proxy(self, proxy_url: str) -> ProxyEntry | None:
        """
        Находит ProxyEntry по URL за O(1) через словарь.

        Args:
            proxy_url: URL прокси, полученный из get_next().

        Returns:
            ProxyEntry или None, если прокси не найден.
        """
        return self._by_url.get(proxy_url)

    def mark_soft_fail(self, proxy_url: str) -> None:
        """
        Регистрирует мягкую ошибку (таймаут, 502/503 от сайта).
        Увеличивает счётчик последовательных ошибок.
        Прокси уходит в cooldown только после max_fails ошибок подряд.
        Это предотвращает каскадное отключение всех прокси
        при временной недоступности целевого сайта.

        Args:
            proxy_url: URL прокси, который вернул get_next().
        """
        if not proxy_url:
            return

        with self._lock:
            proxy = self._find_proxy(proxy_url)
            if proxy is None:
                return

            proxy.fail_count += 1

            if proxy.fail_count >= self._max_fails:
                proxy.failed_at = time.monotonic()
                logger.warning(
                    "Прокси отправлен в cooldown после серии ошибок",
                    extra={"context": {
                        "proxy": str(proxy),
                        "fail_count": proxy.fail_count,
                        "max_fails": self._max_fails,
                        "cooldown_seconds": self._cooldown,
                    }},
                )
            else:
                logger.debug(
                    "Мягкая ошибка прокси (счётчик увеличен)",
                    extra={"context": {
                        "proxy": str(proxy),
                        "fail_count": proxy.fail_count,
                        "max_fails": self._max_fails,
                    }},
                )

    def mark_hard_fail(self, proxy_url: str) -> None:
        """
        Регистрирует жёсткую ошибку (407, ClientProxyConnectionError,
        ClientHttpProxyError). Прокси сразу уходит в cooldown —
        эти ошибки однозначно указывают на проблему с прокси-сервером.

        Args:
            proxy_url: URL прокси, который вернул get_next().
        """
        if not proxy_url:
            return

        with self._lock:
            proxy = self._find_proxy(proxy_url)
            if proxy is None:
                return

            proxy.failed_at = time.monotonic()
            logger.warning(
                "Прокси отправлен в cooldown (жёсткая ошибка)",
                extra={"context": {
                    "proxy": str(proxy),
                    "fail_count": proxy.fail_count,
                    "cooldown_seconds": self._cooldown,
                }},
            )

    def mark_success(self, proxy_url: str) -> None:
        """
        Сбрасывает счётчик ошибок после успешного запроса.
        Вызывается при получении корректного ответа через этот прокси.
        Если прокси был в предтревожном состоянии (fail_count > 0,
        но ещё не в cooldown), один успех обнуляет счётчик.

        Args:
            proxy_url: URL прокси, который вернул get_next().
        """
        if not proxy_url:
            return

        with self._lock:
            proxy = self._find_proxy(proxy_url)
            if proxy is None:
                return

            if proxy.fail_count > 0:
                logger.debug(
                    "Счётчик ошибок прокси сброшен после успеха",
                    extra={"context": {
                        "proxy": str(proxy),
                        "was_fail_count": proxy.fail_count,
                    }},
                )
                proxy.fail_count = 0

    def get_status(self) -> list[dict[str, str | bool | int]]:
        """
        Возвращает текущее состояние всех прокси (для диагностики).

        Returns:
            Список словарей с информацией о каждом прокси.
        """
        now = time.monotonic()
        result: list[dict[str, str | bool | int]] = []

        with self._lock:
            for proxy in self._proxies:
                cooldown_expired = (
                    proxy.is_in_cooldown
                    and (now - proxy.failed_at) >= self._cooldown
                )
                is_available = not proxy.is_in_cooldown or cooldown_expired
                result.append({
                    "proxy": str(proxy),
                    "available": is_available,
                    "in_cooldown": proxy.is_in_cooldown and not cooldown_expired,
                    "fail_count": proxy.fail_count,
                })

        return result

    # ═════════════════════════════════════════════════════════════════════
    # Preflight-проверка прокси с целевым сайтом
    # ═════════════════════════════════════════════════════════════════════

    async def preflight_check(
        self,
        target_url: str,
        *,
        timeout: int = 15,
    ) -> PreflightResult:
        """
        Проверяет работоспособность каждого прокси с целевым сайтом.

        Делает один GET-запрос к target_url через каждый прокси параллельно.
        Прокси считается рабочим, если получен HTTP-ответ с кодом < 500
        (даже 403/404 означает, что прокси доставил запрос до сервера).
        Нерабочие прокси сразу отправляются в hard-fail cooldown.

        Проверяются только активные прокси (не в cooldown).

        Args:
            target_url: URL целевого сайта (главная страница).
            timeout: таймаут одного запроса в секундах.

        Returns:
            PreflightResult со статистикой проверки.
        """
        import aiohttp

        if not self._proxies:
            logger.info("Preflight-проверка пропущена: прокси не настроены")
            return PreflightResult(total=0, passed=0, failed=0)

        # Собираем только активные прокси (не в cooldown)
        now = time.monotonic()
        active_proxies: list[ProxyEntry] = []
        with self._lock:
            for proxy in self._proxies:
                if not proxy.is_in_cooldown:
                    active_proxies.append(proxy)
                elif (now - proxy.failed_at) >= self._cooldown:
                    # Cooldown истёк — восстанавливаем для проверки
                    proxy.failed_at = 0.0
                    proxy.fail_count = 0
                    active_proxies.append(proxy)

        if not active_proxies:
            logger.warning("Preflight-проверка: все прокси уже в cooldown")
            return PreflightResult(
                total=len(self._proxies),
                passed=0,
                failed=len(self._proxies),
            )

        logger.info(
            "Запуск preflight-проверки прокси",
            extra={"context": {
                "target_url": target_url,
                "proxies_to_check": len(active_proxies),
                "timeout": timeout,
            }},
        )

        # Параллельная проверка всех прокси
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        results: list[dict[str, str | bool]] = []
        passed = 0
        failed = 0

        async def _check_one_proxy(proxy: ProxyEntry) -> dict[str, str | bool]:
            """Проверяет один прокси одним GET-запросом к целевому сайту."""
            proxy_label = str(proxy)
            proxy_url = proxy.url

            try:
                async with aiohttp.ClientSession(
                    timeout=client_timeout,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                ) as session:
                    async with session.get(
                        target_url,
                        proxy=proxy_url,
                        allow_redirects=True,
                        ssl=False,
                    ) as response:
                        if response.status < 500:
                            return {
                                "proxy": proxy_label,
                                "ok": True,
                                "status": str(response.status),
                                "error": "",
                            }
                        else:
                            return {
                                "proxy": proxy_label,
                                "ok": False,
                                "status": str(response.status),
                                "error": f"HTTP {response.status}",
                            }

            except asyncio.TimeoutError:
                return {
                    "proxy": proxy_label,
                    "ok": False,
                    "status": "",
                    "error": "Таймаут соединения",
                }
            except aiohttp.ClientProxyConnectionError as exc:
                return {
                    "proxy": proxy_label,
                    "ok": False,
                    "status": "",
                    "error": f"Ошибка подключения к прокси: {exc}",
                }
            except aiohttp.ClientHttpProxyError as exc:
                return {
                    "proxy": proxy_label,
                    "ok": False,
                    "status": str(exc.status) if hasattr(exc, "status") else "",
                    "error": f"HTTP-ошибка прокси: {exc}",
                }
            except aiohttp.ServerDisconnectedError:
                return {
                    "proxy": proxy_label,
                    "ok": False,
                    "status": "",
                    "error": "Сервер разорвал соединение",
                }
            except aiohttp.ClientError as exc:
                return {
                    "proxy": proxy_label,
                    "ok": False,
                    "status": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            except OSError as exc:
                return {
                    "proxy": proxy_label,
                    "ok": False,
                    "status": "",
                    "error": f"Ошибка сети: {exc}",
                }

        # Запускаем все проверки параллельно
        tasks = [_check_one_proxy(proxy) for proxy in active_proxies]
        check_results = await asyncio.gather(*tasks)

        # Обрабатываем результаты и отправляем нерабочие в cooldown
        for proxy, result in zip(active_proxies, check_results):
            results.append(result)
            if result["ok"]:
                passed += 1
                self.mark_success(proxy.url)
                logger.info(
                    "Preflight: прокси работает",
                    extra={"context": {
                        "proxy": str(proxy),
                        "status": result["status"],
                    }},
                )
            else:
                failed += 1
                self.mark_hard_fail(proxy.url)
                logger.warning(
                    "Preflight: прокси не работает с целевым сайтом",
                    extra={"context": {
                        "proxy": str(proxy),
                        "error": result["error"],
                    }},
                )

        preflight_result = PreflightResult(
            total=len(active_proxies),
            passed=passed,
            failed=failed,
            details=results,
        )

        # Итоговое логирование
        if preflight_result.all_failed:
            logger.warning(
                "Preflight-проверка: ни один прокси не работает с целевым сайтом",
                extra={"context": {
                    "target_url": target_url,
                    "total": preflight_result.total,
                }},
            )
        else:
            logger.info(
                "Preflight-проверка завершена",
                extra={"context": {
                    "target_url": target_url,
                    "passed": passed,
                    "failed": failed,
                    "total": len(active_proxies),
                }},
            )

        return preflight_result
