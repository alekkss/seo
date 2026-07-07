"""
Структурированное JSON-логирование.

Формат каждой записи:
    {"timestamp": "...", "level": "INFO", "message": "...", "trace_id": "...", "context": {...}}

Вывод дублируется в stdout и в файл с ротацией.

Использование:
    from site_audit.config.logger import get_logger, setup_logging

    setup_logging()  # вызвать один раз при старте
    logger = get_logger("service.audit")
    logger.info("Аудит запущен", extra={"context": {"url": "https://example.com"}})
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


# ── Хранилище trace_id для текущего контекста ─────────────────

_current_trace_id: str = ""


def set_trace_id(trace_id: str | None = None) -> str:
    """
    Устанавливает trace_id для текущего контекста логирования.

    Args:
        trace_id: идентификатор трассировки. Если None — генерируется новый.

    Returns:
        Установленный trace_id.
    """
    global _current_trace_id  # noqa: PLW0603
    _current_trace_id = trace_id or uuid.uuid4().hex[:12]
    return _current_trace_id


def get_trace_id() -> str:
    """Возвращает текущий trace_id."""
    return _current_trace_id


# ── JSON-форматтер ────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """Форматирует записи лога в JSON с полями timestamp, level, message, trace_id, context."""

    def format(self, record: logging.LogRecord) -> str:
        """
        Формирует JSON-строку из записи лога.

        Args:
            record: стандартная запись logging.

        Returns:
            JSON-строка с полями лога.
        """
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": _current_trace_id,
        }

        # Контекст из extra
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            log_entry["context"] = context
        elif context is not None:
            log_entry["context"] = {"value": str(context)}

        # Информация об исключении
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }
            log_entry["traceback"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False, default=str)


# ── Читаемый форматтер для stdout ────────────────────────────

class ReadableFormatter(logging.Formatter):
    """Человекочитаемый формат для вывода в консоль."""

    COLORS: dict[str, str] = {
        "DEBUG": "\033[36m",     # голубой
        "INFO": "\033[32m",      # зелёный
        "WARNING": "\033[33m",   # жёлтый
        "ERROR": "\033[31m",     # красный
        "CRITICAL": "\033[35m",  # фиолетовый
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """
        Формирует читаемую строку для консоли.

        Args:
            record: стандартная запись logging.

        Returns:
            Отформатированная строка с цветом уровня.
        """
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET if color else ""

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        trace = f" [{_current_trace_id}]" if _current_trace_id else ""

        base = f"{timestamp} {color}{record.levelname:<8}{reset}{trace} {record.name} — {record.getMessage()}"

        context = getattr(record, "context", None)
        if isinstance(context, dict) and context:
            ctx_parts = [f"{k}={v}" for k, v in context.items()]
            base += f" | {', '.join(ctx_parts)}"

        if record.exc_info and record.exc_info[1] is not None:
            base += f"\n{self.formatException(record.exc_info)}"

        return base


# ── Настройка логирования ─────────────────────────────────────

_logging_configured: bool = False


def setup_logging(
    *,
    log_level: str = "INFO",
    log_file_path: str = "./logs/app.log",
    log_max_bytes: int = 10_485_760,
    log_backup_count: int = 5,
) -> None:
    """
    Инициализирует систему логирования.

    Создаёт папку для логов, настраивает два обработчика:
    - stdout с читаемым форматом
    - файл с JSON-форматом и ротацией

    Вызывается один раз при старте приложения.

    Args:
        log_level: уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file_path: путь к файлу логов.
        log_max_bytes: максимальный размер файла лога в байтах.
        log_backup_count: количество файлов ротации.
    """
    global _logging_configured  # noqa: PLW0603

    if _logging_configured:
        return

    level = getattr(logging, log_level.upper(), logging.INFO)

    # Создаём папку для логов
    log_path = Path(log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Корневой логгер проекта
    root_logger = logging.getLogger("site_audit")
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.propagate = False

    # ── Обработчик stdout (читаемый формат) ───────────────────
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(ReadableFormatter())
    root_logger.addHandler(stdout_handler)

    # ── Обработчик файла (JSON с ротацией) ────────────────────
    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=log_max_bytes,
        backupCount=log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(file_handler)

    _logging_configured = True
    root_logger.info(
        "Система логирования инициализирована",
        extra={"context": {"level": log_level, "file": log_file_path}},
    )


def get_logger(name: str) -> logging.Logger:
    """
    Возвращает именованный логгер как дочерний от 'site_audit'.

    Args:
        name: имя логгера (например, 'service.audit', 'bot.handlers').

    Returns:
        Настроенный экземпляр logging.Logger.
    """
    return logging.getLogger(f"site_audit.{name}")
