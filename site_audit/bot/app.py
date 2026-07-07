"""
Точка входа Telegram-бота.

Собирает зависимости, регистрирует обработчики и запускает polling.

Запуск:
    python -m site_audit.bot

Или из кода:
    from site_audit.bot import run_bot
    run_bot()
"""

from __future__ import annotations

from telegram.ext import Application

from site_audit.config.logger import get_logger, setup_logging
from site_audit.config.settings import Settings, get_settings
from site_audit.services.audit_service import AuditService

from .handlers import register_handlers
from .states import SessionManager

logger = get_logger("bot.app")


def _create_application(settings: Settings) -> Application:  # type: ignore[type-arg]
    """
    Создаёт и настраивает экземпляр Application.

    Args:
        settings: настройки приложения с токеном бота.

    Returns:
        Сконфигурированный экземпляр Application.
    """
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    # ── Создаём зависимости ───────────────────────────────────
    session_manager = SessionManager()
    audit_service = AuditService(settings)

    # ── Регистрируем обработчики ──────────────────────────────
    register_handlers(
        application=application,
        session_manager=session_manager,
        audit_service=audit_service,
        settings=settings,
    )

    logger.info(
        "Приложение бота создано",
        extra={"context": {
            "allowed_users": len(settings.allowed_user_ids),
            "all_access": len(settings.allowed_user_ids) == 0,
        }},
    )

    return application


def run_bot() -> None:
    """
    Запускает Telegram-бота в режиме polling.

    Загружает конфигурацию, инициализирует логирование,
    создаёт все зависимости и запускает бесконечный цикл
    получения обновлений от Telegram API.

    Raises:
        ValueError: если конфигурация невалидна
                    (например, отсутствует TELEGRAM_BOT_TOKEN).
        SystemExit: при критической ошибке запуска.
    """
    # ── Загрузка конфигурации ─────────────────────────────────
    try:
        settings = get_settings()
    except ValueError as exc:
        print(f"Ошибка конфигурации: {exc}")
        print("Проверьте файл .env и переменные окружения.")
        raise SystemExit(1) from exc

    # ── Настройка логирования ─────────────────────────────────
    setup_logging(
        log_level=settings.log_level,
        log_file_path=settings.log_file_path,
        log_max_bytes=settings.log_max_bytes,
        log_backup_count=settings.log_backup_count,
    )

    logger.info(
        "Запуск Telegram-бота",
        extra={"context": {
            "log_level": settings.log_level,
            "output_dir": settings.output_dir,
        }},
    )

    # ── Создание и запуск приложения ──────────────────────────
    application = _create_application(settings)

    logger.info("Бот запущен, ожидание сообщений...")
    print("Бот запущен. Нажмите Ctrl+C для остановки.")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


# Позволяет запускать бота как python -m site_audit.bot
if __name__ == "__main__":
    run_bot()
