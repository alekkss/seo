"""
Централизованная конфигурация приложения.
Загружает переменные окружения из .env файла, выполняет ручную валидацию
обязательных параметров и предоставляет доступ через dataclass Settings.
Использование:
    from site_audit.config.settings import get_settings
    settings = get_settings()
    print(settings.telegram_bot_token)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Неизменяемый объект конфигурации приложения."""

    # ── Telegram-бот ──────────────────────────────────────────
    telegram_bot_token: str
    allowed_user_ids: list[int] = field(default_factory=list)

    # ── Прокси ────────────────────────────────────────────────
    proxy_enabled: bool = False
    proxy_file_path: str = "./proxies.txt"
    proxy_cooldown: float = 120.0
    proxy_max_fails: int = 3
    proxy_max_connections: int = 3

    # ── Параметры аудита ──────────────────────────────────────
    default_max_crawl_pages: int = 500
    default_max_depth: int = 3
    default_limit: int = 0
    default_workers: int = 20
    default_delay: float = 0.0

    # Опциональный предохранитель от переполнения памяти: максимальное
    # общее количество страниц для аудита. Применяется НЕЗАВИСИМО от
    # источника URL (sitemap или BFS-обход).
    # 0 — без лимита (по умолчанию). Установите значение > 0 через
    # DEFAULT_MAX_TOTAL_PAGES в .env, если сервер ограничен по памяти.
    default_max_total_pages: int = 0

    # Сетевые настройки (увеличены для устойчивости к медленным серверам)
    default_timeout: int = 30
    default_connect_timeout: int = 10
    default_retries: int = 3

    default_min_text_length: int = 100
    default_max_image_size_kb: int = 500
    default_check_external_links: bool = False

    # ── Отчёты ────────────────────────────────────────────────
    output_dir: str = "./reports"
    excel_report_name: str = "audit_report.xlsx"
    html_report_name: str = "audit_report.html"

    # ── Логирование ───────────────────────────────────────────
    log_level: str = "INFO"
    log_file_path: str = "./logs/app.log"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5


def _parse_bool(value: str) -> bool:
    """Преобразует строковое значение переменной окружения в bool."""
    return value.strip().lower() in ("true", "1", "yes", "on")


def _parse_allowed_user_ids(raw: str) -> list[int]:
    """Парсит строку с ID пользователей в список int."""
    if not raw or not raw.strip():
        return []
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ValueError(
                f"Некорректный ID пользователя в ALLOWED_USER_IDS: '{part}'. "
                f"Ожидается целое число."
            ) from exc
    return result


def _validate_positive_int(value: int, name: str) -> None:
    """Проверяет, что значение — неотрицательное целое число."""
    if value < 0:
        raise ValueError(
            f"Переменная {name} не может быть отрицательной: {value}"
        )


def _validate_log_level(level: str) -> None:
    """Проверяет корректность уровня логирования."""
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    if level.upper() not in valid_levels:
        raise ValueError(
            f"Некорректный уровень логирования LOG_LEVEL='{level}'. "
            f"Допустимые значения: {', '.join(valid_levels)}"
        )


def _load_settings() -> Settings:
    """
    Загружает переменные окружения и создаёт объект Settings.

    Raises:
        ValueError: если обязательные переменные отсутствуют
                    или значения некорректны.
    """
    # Загружаем .env (не перезаписывает уже установленные переменные)
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path, override=False)

    # ── Обязательные переменные ───────────────────────────────
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_bot_token:
        raise ValueError(
            "Обязательная переменная TELEGRAM_BOT_TOKEN не задана. "
            "Укажите токен бота в файле .env или в переменных окружения."
        )

    # ── Telegram ──────────────────────────────────────────────
    allowed_user_ids = _parse_allowed_user_ids(
        os.getenv("ALLOWED_USER_IDS", "")
    )

    # ── Прокси ────────────────────────────────────────────────
    proxy_enabled = _parse_bool(os.getenv("PROXY_ENABLED", "false"))
    proxy_file_path = os.getenv("PROXY_FILE_PATH", "./proxies.txt").strip()
    proxy_cooldown = float(os.getenv("PROXY_COOLDOWN", "120"))
    proxy_max_fails = int(os.getenv("PROXY_MAX_FAILS", "3"))
    proxy_max_connections = int(os.getenv("PROXY_MAX_CONNECTIONS", "3"))

    # ── Параметры аудита ──────────────────────────────────────
    default_max_crawl_pages = int(os.getenv("DEFAULT_MAX_CRAWL_PAGES", "500"))
    default_max_depth = int(os.getenv("DEFAULT_MAX_DEPTH", "3"))
    default_limit = int(os.getenv("DEFAULT_LIMIT", "0"))
    default_workers = int(os.getenv("DEFAULT_WORKERS", "20"))
    default_delay = float(os.getenv("DEFAULT_DELAY", "0.0"))

    # Опциональный предохранитель: 0 = без лимита
    default_max_total_pages = int(
        os.getenv("DEFAULT_MAX_TOTAL_PAGES", "0")
    )

    # Сетевые настройки
    default_timeout = int(os.getenv("DEFAULT_TIMEOUT", "30"))
    default_connect_timeout = int(os.getenv("DEFAULT_CONNECT_TIMEOUT", "10"))
    default_retries = int(os.getenv("DEFAULT_RETRIES", "3"))

    default_min_text_length = int(os.getenv("DEFAULT_MIN_TEXT_LENGTH", "100"))
    default_max_image_size_kb = int(os.getenv("DEFAULT_MAX_IMAGE_SIZE_KB", "500"))
    default_check_external_links = _parse_bool(
        os.getenv("DEFAULT_CHECK_EXTERNAL_LINKS", "false")
    )

    # ── Отчёты ────────────────────────────────────────────────
    output_dir = os.getenv("OUTPUT_DIR", "./reports")
    excel_report_name = os.getenv("EXCEL_REPORT_NAME", "audit_report.xlsx")
    html_report_name = os.getenv("HTML_REPORT_NAME", "audit_report.html")

    # ── Логирование ───────────────────────────────────────────
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file_path = os.getenv("LOG_FILE_PATH", "./logs/app.log")
    log_max_bytes = int(os.getenv("LOG_MAX_BYTES", "10485760"))
    log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))

    # ── Валидация значений ────────────────────────────────────
    _validate_positive_int(default_max_crawl_pages, "DEFAULT_MAX_CRAWL_PAGES")
    _validate_positive_int(default_max_depth, "DEFAULT_MAX_DEPTH")
    _validate_positive_int(default_limit, "DEFAULT_LIMIT")
    _validate_positive_int(default_workers, "DEFAULT_WORKERS")
    _validate_positive_int(default_max_total_pages, "DEFAULT_MAX_TOTAL_PAGES")
    _validate_positive_int(default_timeout, "DEFAULT_TIMEOUT")
    _validate_positive_int(default_connect_timeout, "DEFAULT_CONNECT_TIMEOUT")
    _validate_positive_int(default_retries, "DEFAULT_RETRIES")
    _validate_positive_int(default_min_text_length, "DEFAULT_MIN_TEXT_LENGTH")
    _validate_positive_int(default_max_image_size_kb, "DEFAULT_MAX_IMAGE_SIZE_KB")
    _validate_positive_int(log_max_bytes, "LOG_MAX_BYTES")
    _validate_positive_int(log_backup_count, "LOG_BACKUP_COUNT")
    _validate_log_level(log_level)

    if default_workers == 0:
        raise ValueError(
            "Переменная DEFAULT_WORKERS должна быть >= 1."
        )
    if default_delay < 0:
        raise ValueError(
            f"Переменная DEFAULT_DELAY не может быть отрицательной: {default_delay}"
        )
    if default_timeout < 5:
        raise ValueError(
            f"Переменная DEFAULT_TIMEOUT должна быть >= 5 секунд: {default_timeout}"
        )
    if default_retries < 0:
        raise ValueError(
            f"Переменная DEFAULT_RETRIES не может быть отрицательной: {default_retries}"
        )

    # Валидация прокси
    if proxy_cooldown < 0:
        raise ValueError(
            f"Переменная PROXY_COOLDOWN не может быть отрицательной: {proxy_cooldown}"
        )
    if proxy_enabled and not proxy_file_path:
        raise ValueError(
            "PROXY_ENABLED=true, но PROXY_FILE_PATH не указан. "
            "Укажите путь к файлу со списком прокси."
        )
    if proxy_max_fails < 1:
        raise ValueError(
            f"Переменная PROXY_MAX_FAILS должна быть >= 1: {proxy_max_fails}"
        )
    if proxy_max_connections < 1:
        raise ValueError(
            f"Переменная PROXY_MAX_CONNECTIONS должна быть >= 1: {proxy_max_connections}"
        )

    return Settings(
        telegram_bot_token=telegram_bot_token,
        allowed_user_ids=allowed_user_ids,
        proxy_enabled=proxy_enabled,
        proxy_file_path=proxy_file_path,
        proxy_cooldown=proxy_cooldown,
        proxy_max_fails=proxy_max_fails,
        proxy_max_connections=proxy_max_connections,
        default_max_crawl_pages=default_max_crawl_pages,
        default_max_depth=default_max_depth,
        default_limit=default_limit,
        default_workers=default_workers,
        default_delay=default_delay,
        default_max_total_pages=default_max_total_pages,
        default_timeout=default_timeout,
        default_connect_timeout=default_connect_timeout,
        default_retries=default_retries,
        default_min_text_length=default_min_text_length,
        default_max_image_size_kb=default_max_image_size_kb,
        default_check_external_links=default_check_external_links,
        output_dir=output_dir,
        excel_report_name=excel_report_name,
        html_report_name=html_report_name,
        log_level=log_level,
        log_file_path=log_file_path,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )


# ── Кэшированный синглтон ────────────────────────────────────

_settings_instance: Settings | None = None


def get_settings(*, force_reload: bool = False) -> Settings:
    """
    Возвращает объект настроек (синглтон).
    При первом вызове загружает и валидирует конфигурацию.
    Последующие вызовы возвращают закэшированный экземпляр.

    Args:
        force_reload: принудительно перечитать конфигурацию.

    Returns:
        Объект Settings с текущими настройками.

    Raises:
        ValueError: если обязательные переменные не заданы
                    или значения некорректны.
    """
    global _settings_instance  # noqa: PLW0603
    if _settings_instance is None or force_reload:
        _settings_instance = _load_settings()
    return _settings_instance
