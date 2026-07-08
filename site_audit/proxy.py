"""
Управление пулом прокси-серверов: загрузка из файла, round-robin ротация,
мягкий и жёсткий отказ, автоматическое восстановление через cooldown,
ограничение одновременных соединений на каждый прокси.

Два типа ошибок:
  - mark_hard_fail() — проблема прокси (407, ClientProxyConnectionError):
    прокси сразу уходит в cooldown.
  - mark_soft_fail() — возможно проблема сайта (таймаут, 502/503):
    увеличивается счётчик ошибок, cooldown только после max_fails подряд.

Использование:
    rotator = ProxyRotator.from_file("proxies.txt", cooldown=120, max_fails=3)
    proxy_url = rotator.get_next()       # "http://login:password@host:port" или None
    sem = rotator.get_semaphore(proxy_url)  # семафор для этого прокси
    rotator.mark_soft_fail(proxy_url)    # мягкая ошибка (таймаут)
    rotator.mark_hard_fail(proxy_url)    # жёсткая ошибка (прокси мёртв)
    rotator.mark_success(proxy_url)      # успех — сброс счётчика
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


class ProxyRotator:
    """
    Round-robin ротатор прокси с мягким/жёстким отказом и семафорами.

    Принцип работы:
    - Прокси выдаются по кругу (round-robin) через потокобезопасный индекс.
    - mark_soft_fail() увеличивает счётчик ошибок; cooldown наступает
      только после max_fails последовательных ошибок.
    - mark_hard_fail() сразу отправляет прокси в cooldown (ошибка прокси).
    - mark_success() сбрасывает счётчик ошибок.
    - По истечении cooldown прокси автоматически возвращается в пул
      с обнулённым счётчиком.
    - Каждый прокси имеет свой asyncio.Semaphore для ограничения
      одновременных соединений.
    - Если все прокси в cooldown — возвращает None (запрос пойдёт напрямую).

    Потокобезопасность обеспечена через threading.Lock, что корректно
    работает и в asyncio (Lock захватывается на микросекунды без I/O).
    """

    def __init__(
        self,
        proxies: list[ProxyEntry],
        *,
        cooldown: float = 120.0,
        max_fails: int = 3,
        max_connections: int = 5,
    ) -> None:
        """
        Инициализирует ротатор с готовым списком прокси.

        Args:
            proxies: список прокси-серверов.
            cooldown: время в секундах, на которое нерабочий прокси
                      исключается из ротации.
            max_fails: количество последовательных мягких ошибок до cooldown.
            max_connections: максимум одновременных соединений на один прокси.
        """
        self._proxies: list[ProxyEntry] = proxies
        self._cooldown: float = cooldown
        self._max_fails: int = max_fails
        self._max_connections: int = max_connections
        self._index: int = 0
        self._lock: threading.Lock = threading.Lock()

        # Семафор для каждого прокси — ограничивает одновременные соединения
        self._semaphores: dict[str, asyncio.Semaphore] = {
            proxy.url: asyncio.Semaphore(max_connections)
            for proxy in proxies
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
        max_connections: int = 5,
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

    def get_semaphore(self, proxy_url: str | None) -> asyncio.Semaphore | None:
        """
        Возвращает семафор для указанного прокси.
        Используется в async_fetch для ограничения одновременных
        соединений через конкретный прокси-сервер.

        Args:
            proxy_url: URL прокси, полученный из get_next().

        Returns:
            asyncio.Semaphore для этого прокси или None.
        """
        if not proxy_url:
            return None
        return self._semaphores.get(proxy_url)

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
            for proxy in self._proxies:
                if proxy.url == proxy_url:
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
                    return

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
            for proxy in self._proxies:
                if proxy.url == proxy_url:
                    proxy.failed_at = time.monotonic()
                    logger.warning(
                        "Прокси отправлен в cooldown (жёсткая ошибка)",
                        extra={"context": {
                            "proxy": str(proxy),
                            "fail_count": proxy.fail_count,
                            "cooldown_seconds": self._cooldown,
                        }},
                    )
                    return

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
            for proxy in self._proxies:
                if proxy.url == proxy_url:
                    if proxy.fail_count > 0:
                        logger.debug(
                            "Счётчик ошибок прокси сброшен после успеха",
                            extra={"context": {
                                "proxy": str(proxy),
                                "was_fail_count": proxy.fail_count,
                            }},
                        )
                        proxy.fail_count = 0
                    return

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
