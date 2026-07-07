"""
Обработчики команд и callback-запросов Telegram-бота.

Маршрутизация callback-данных по префиксам:
    menu:*       → навигация по меню
    check:*      → выбор проверок
    setting:*    → настройка параметров
    audit:*      → запуск аудита

Использование:
    from site_audit.bot.handlers import register_handlers

    register_handlers(application, session_manager, audit_service, settings)
"""

from __future__ import annotations

import threading
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from site_audit.config.logger import get_logger
from site_audit.config.settings import Settings
from site_audit.services.audit_service import AuditParams, AuditService

from .keyboards import (
    CB_CANCEL,
    CB_CHECKS_ALL_OFF,
    CB_CHECKS_ALL_ON,
    CB_CHECKS_BACK,
    CB_NEW_AUDIT,
    CB_RUN_AUDIT,
    CB_SELECT_CHECKS,
    CB_SETTINGS,
    CB_SETTINGS_BACK,
    CB_SETTINGS_EXTERNAL,
    PREFIX_CHECK,
    PREFIX_SETTING,
    build_audit_done_keyboard,
    build_audit_running_keyboard,
    build_checks_keyboard,
    build_main_menu,
    build_setting_input_keyboard,
    build_settings_keyboard,
    build_start_keyboard,
    format_session_summary,
)
from .states import SETTINGS_BY_KEY, SessionManager, UserSession, UserState

logger = get_logger("bot.handlers")


# ── Безопасные обёртки для редактирования сообщений ───────────

async def _safe_edit_text(
    query: Any,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Редактирует текст сообщения, игнорируя ошибку «message is not modified».

    Args:
        query: callback-запрос.
        text: новый текст сообщения.
        parse_mode: режим форматирования (Markdown, HTML).
        reply_markup: inline-клавиатура.
    """
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _safe_edit_markup(
    query: Any,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """
    Редактирует клавиатуру сообщения, игнорируя ошибку «message is not modified».

    Args:
        query: callback-запрос.
        reply_markup: новая inline-клавиатура.
    """
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


# ── Проверка доступа ──────────────────────────────────────────

def _is_authorized(user_id: int, settings: Settings) -> bool:
    """
    Проверяет, разрешён ли пользователю доступ к боту.

    Args:
        user_id: Telegram ID пользователя.
        settings: настройки приложения.

    Returns:
        True, если доступ разрешён.
    """
    if not settings.allowed_user_ids:
        return True
    return user_id in settings.allowed_user_ids


# ── Команды ───────────────────────────────────────────────────

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обработчик команды /start."""
    if update.effective_user is None or update.message is None:
        return

    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    session_manager: SessionManager = context.bot_data["session_manager"]

    if not _is_authorized(user_id, settings):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        logger.warning(
            "Неавторизованный доступ",
            extra={"context": {"user_id": user_id}},
        )
        return

    session_manager.reset(user_id)

    logger.info(
        "Пользователь запустил бота",
        extra={"context": {"user_id": user_id}},
    )

    await update.message.reply_text(
        "👋 *Добро пожаловать в Site Audit Bot!*\n\n"
        "Я помогу провести комплексный аудит вашего сайта:\n"
        "• Пустые страницы и битые ссылки\n"
        "• SEO-проблемы и дубликаты\n"
        "• Тяжёлые картинки и редиректы\n"
        "• Заглушки и placeholder-тексты\n\n"
        "Нажмите кнопку ниже, чтобы начать.",
        parse_mode="Markdown",
        reply_markup=build_start_keyboard(),
    )


async def cmd_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обработчик команды /help."""
    if update.message is None:
        return

    await update.message.reply_text(
        "📖 *Как пользоваться ботом:*\n\n"
        "1️⃣ Нажмите «Начать аудит»\n"
        "2️⃣ Отправьте URL сайта (например, `https://example.com`)\n"
        "3️⃣ Выберите нужные проверки и настройте параметры\n"
        "4️⃣ Нажмите «Запустить аудит»\n"
        "5️⃣ Дождитесь завершения и получите отчёты\n\n"
        "*Команды:*\n"
        "/start — начать сначала\n"
        "/help — эта справка\n",
        parse_mode="Markdown",
    )


# ── Обработка текстовых сообщений ─────────────────────────────

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обработчик текстовых сообщений (URL и значения настроек)."""
    if update.effective_user is None or update.message is None:
        return

    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    session_manager: SessionManager = context.bot_data["session_manager"]

    if not _is_authorized(user_id, settings):
        return

    session = session_manager.get(user_id)
    text = update.message.text or ""

    if session.state == UserState.WAITING_URL:
        await _handle_url_input(update, session, text)

    elif session.state == UserState.WAITING_SETTING_VALUE:
        await _handle_setting_input(update, session, text)

    else:
        await update.message.reply_text(
            "Нажмите /start, чтобы начать аудит.",
            reply_markup=build_start_keyboard(),
        )


async def _handle_url_input(
    update: Update,
    session: UserSession,
    text: str,
) -> None:
    """Обрабатывает ввод URL от пользователя."""
    if update.message is None:
        return

    url = text.strip()

    if not url:
        await update.message.reply_text("❌ URL не может быть пустым. Попробуйте ещё раз:")
        return

    if " " in url:
        await update.message.reply_text("❌ URL не должен содержать пробелов. Попробуйте ещё раз:")
        return

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if "." not in url.split("//", 1)[-1]:
        await update.message.reply_text(
            "❌ Некорректный URL. Укажите домен, например: `example.com`",
            parse_mode="Markdown",
        )
        return

    session.url = url
    session.state = UserState.MENU

    logger.info(
        "Пользователь ввёл URL",
        extra={"context": {"user_id": session.user_id, "url": url}},
    )

    summary = format_session_summary(session)
    await update.message.reply_text(
        f"{summary}\n"
        f"Выберите действие:",
        parse_mode="Markdown",
        reply_markup=build_main_menu(session),
    )


async def _handle_setting_input(
    update: Update,
    session: UserSession,
    text: str,
) -> None:
    """Обрабатывает ввод значения настройки от пользователя."""
    if update.message is None:
        return

    key = session.pending_setting_key
    error = session.set_setting_value(key, text)

    if error:
        await update.message.reply_text(
            f"❌ {error}\nПопробуйте ещё раз или нажмите «Отмена»:",
            reply_markup=build_setting_input_keyboard(),
        )
        return

    session.state = UserState.SETTINGS
    session.pending_setting_key = ""

    meta = SETTINGS_BY_KEY.get(key)
    label = meta.label if meta else key

    logger.info(
        "Параметр изменён",
        extra={"context": {
            "user_id": session.user_id,
            "key": key,
            "value": session.get_setting_value(key),
        }},
    )

    await update.message.reply_text(
        f"✅ *{label}* установлен: `{session.get_setting_value(key)}`",
        parse_mode="Markdown",
        reply_markup=build_settings_keyboard(session),
    )


# ── Обработка callback-кнопок ─────────────────────────────────

async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Главный роутер callback-запросов от inline-кнопок."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    await query.answer()

    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    session_manager: SessionManager = context.bot_data["session_manager"]

    if not _is_authorized(user_id, settings):
        await _safe_edit_text(query, "⛔ У вас нет доступа к этому боту.")
        return

    session = session_manager.get(user_id)
    data = query.data or ""

    # Игнорируем noop-кнопки
    if data == "noop":
        return

    # Блокируем действия во время аудита
    if session.state == UserState.AUDIT_RUNNING and data != CB_CANCEL:
        await query.answer("⏳ Дождитесь завершения аудита.", show_alert=True)
        return

    # ── Маршрутизация по callback-данным ──────────────────────
    # ВАЖНО: точные совпадения проверяются ДО startswith,
    # чтобы "check:back" не попало в обработку toggle проверки.

    if data == CB_NEW_AUDIT:
        await _cb_new_audit(query, session)

    elif data == CB_SELECT_CHECKS:
        await _cb_select_checks(query, session)

    elif data == CB_CHECKS_BACK:
        await _cb_back_to_menu(query, session)

    elif data == CB_CHECKS_ALL_ON:
        await _cb_checks_all(query, session, enabled=True)

    elif data == CB_CHECKS_ALL_OFF:
        await _cb_checks_all(query, session, enabled=False)

    elif data.startswith(f"{PREFIX_CHECK}:"):
        await _cb_check_toggle(query, session, data)

    elif data == CB_SETTINGS:
        await _cb_settings(query, session)

    elif data == CB_SETTINGS_BACK:
        await _cb_back_to_menu(query, session)

    elif data == CB_SETTINGS_EXTERNAL:
        await _cb_toggle_external(query, session)

    elif data.startswith(f"{PREFIX_SETTING}:"):
        await _cb_setting_select(query, session, data)

    elif data == CB_RUN_AUDIT:
        await _cb_run_audit(query, session, context)

    elif data == CB_CANCEL:
        await _cb_cancel(query, session, session_manager)

    else:
        logger.warning(
            "Неизвестный callback",
            extra={"context": {"user_id": user_id, "data": data}},
        )


# ── Обработчики конкретных callback-ов ────────────────────────

async def _cb_new_audit(query: Any, session: UserSession) -> None:
    """Начало нового аудита — запрашиваем URL."""
    session.reset()
    session.state = UserState.WAITING_URL

    await _safe_edit_text(
        query,
        "🌐 Отправьте URL сайта для аудита.\n\n"
        "Пример: `https://example.com`",
        parse_mode="Markdown",
    )


async def _cb_select_checks(query: Any, session: UserSession) -> None:
    """Переход к экрану выбора проверок."""
    session.state = UserState.SELECTING_CHECKS

    await _safe_edit_text(
        query,
        "📋 *Выберите проверки:*\n\n"
        "Нажимайте на проверку, чтобы включить или выключить её.",
        parse_mode="Markdown",
        reply_markup=build_checks_keyboard(session),
    )


async def _cb_check_toggle(query: Any, session: UserSession, data: str) -> None:
    """Переключение одной проверки."""
    check_name = data.split(":", 1)[1]
    session.toggle_check(check_name)

    await _safe_edit_markup(query, reply_markup=build_checks_keyboard(session))


async def _cb_checks_all(query: Any, session: UserSession, *, enabled: bool) -> None:
    """Включение или выключение всех проверок."""
    for name in session.selected_checks:
        session.selected_checks[name] = enabled

    await _safe_edit_markup(query, reply_markup=build_checks_keyboard(session))


async def _cb_back_to_menu(query: Any, session: UserSession) -> None:
    """Возврат в главное меню."""
    session.state = UserState.MENU
    session.pending_setting_key = ""

    summary = format_session_summary(session)
    await _safe_edit_text(
        query,
        f"{summary}\n"
        f"Выберите действие:",
        parse_mode="Markdown",
        reply_markup=build_main_menu(session),
    )


async def _cb_settings(query: Any, session: UserSession) -> None:
    """Переход к экрану настроек."""
    session.state = UserState.SETTINGS

    await _safe_edit_text(
        query,
        "⚙️ *Настройки аудита:*\n\n"
        "Нажмите на параметр, чтобы изменить его значение.",
        parse_mode="Markdown",
        reply_markup=build_settings_keyboard(session),
    )


async def _cb_setting_select(query: Any, session: UserSession, data: str) -> None:
    """Пользователь выбрал параметр для редактирования."""
    key = data.split(":", 1)[1]

    if key == "back":
        await _cb_back_to_menu(query, session)
        return

    meta = SETTINGS_BY_KEY.get(key)
    if meta is None:
        return

    session.state = UserState.WAITING_SETTING_VALUE
    session.pending_setting_key = key

    current_value = session.get_setting_value(key)
    bounds = ""
    if meta.min_value is not None and meta.max_value is not None:
        bounds = f"от {meta.min_value} до {meta.max_value}"
    elif meta.min_value is not None:
        bounds = f"от {meta.min_value}"

    await _safe_edit_text(
        query,
        f"⚙️ *{meta.label}*\n\n"
        f"{meta.description}\n"
        f"Текущее значение: `{current_value}`\n"
        f"Допустимые значения: {bounds}\n\n"
        f"Отправьте новое значение:",
        parse_mode="Markdown",
        reply_markup=build_setting_input_keyboard(),
    )


async def _cb_toggle_external(query: Any, session: UserSession) -> None:
    """Переключение проверки внешних ссылок."""
    session.check_external_links = not session.check_external_links

    logger.info(
        "Переключение внешних ссылок",
        extra={"context": {
            "user_id": session.user_id,
            "check_external_links": session.check_external_links,
        }},
    )

    await _safe_edit_markup(query, reply_markup=build_settings_keyboard(session))


async def _cb_run_audit(
    query: Any,
    session: UserSession,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Запуск аудита в фоновом потоке."""
    if not session.url:
        await _safe_edit_text(
            query,
            "❌ URL не задан. Начните заново.",
            reply_markup=build_start_keyboard(),
        )
        return

    enabled_checks = session.get_enabled_checks()
    if not enabled_checks:
        await query.answer("⚠️ Выберите хотя бы одну проверку!", show_alert=True)
        return

    session.state = UserState.AUDIT_RUNNING

    await _safe_edit_text(
        query,
        f"⏳ *Аудит запущен*\n\n"
        f"🌐 URL: `{session.url}`\n"
        f"📋 Проверок: {len(enabled_checks)}\n\n"
        f"Это может занять несколько минут...",
        parse_mode="Markdown",
        reply_markup=build_audit_running_keyboard(),
    )

    logger.info(
        "Аудит запущен из бота",
        extra={"context": {
            "user_id": session.user_id,
            "url": session.url,
            "checks": enabled_checks,
        }},
    )

    audit_service: AuditService = context.bot_data["audit_service"]
    settings: Settings = context.bot_data["settings"]
    chat_id = query.message.chat_id

    params = AuditParams(
        base_url=session.url,
        check_names=enabled_checks,
        max_crawl_pages=settings.default_max_crawl_pages,
        max_depth=session.max_depth,
        limit=session.limit,
        workers=session.workers,
        delay=session.delay,
        timeout=session.timeout,
        min_text_length=session.min_text_length,
        max_image_size_kb=session.max_image_size_kb,
        check_external_links=session.check_external_links,
        output_dir=settings.output_dir,
        excel_name=settings.excel_report_name,
        html_name=settings.html_report_name,
    )

    thread = threading.Thread(
        target=_run_audit_thread,
        args=(context.application, chat_id, session, audit_service, params),
        daemon=True,
    )
    thread.start()


def _run_audit_thread(
    application: Application,  # type: ignore[type-arg]
    chat_id: int,
    session: UserSession,
    audit_service: AuditService,
    params: AuditParams,
) -> None:
    """
    Выполняет аудит в фоновом потоке.

    Отправляет сообщения о прогрессе и результатах в чат через job_queue.
    """
    progress_messages: list[str] = []

    def on_progress(message: str) -> None:
        """Callback прогресса — накапливает сообщения."""
        progress_messages.append(message)
        if len(progress_messages) % 3 == 0:
            _schedule_message(application, chat_id, message)

    try:
        result = audit_service.run_audit(params, on_progress=on_progress)

        summary_lines = [
            "✅ *Аудит завершён!*\n",
            f"🌐 Сайт: `{result.base_url}`",
            f"📄 Страниц проверено: {result.total_urls}",
            f"🐛 Проблем найдено: {result.total_issues}",
            f"⏱ Время: {result.elapsed_seconds} сек\n",
            "📊 *Результаты по проверкам:*",
        ]

        for name in params.check_names:
            count = len(result.results.get(name, []))
            marker = "✅" if count == 0 else "❌"
            description = audit_service.available_checks().get(name, name)
            summary_lines.append(f"  {marker} {description}: {count}")

        summary_text = "\n".join(summary_lines)

        _schedule_message(application, chat_id, summary_text, parse_mode="Markdown")
        _schedule_file(application, chat_id, result.excel_path, "📊 Excel-отчёт")
        _schedule_file(application, chat_id, result.html_path, "📄 HTML-отчёт")
        _schedule_keyboard(application, chat_id, build_audit_done_keyboard())

        logger.info(
            "Аудит из бота завершён успешно",
            extra={"context": {
                "user_id": session.user_id,
                "url": result.base_url,
                "issues": result.total_issues,
            }},
        )

    except Exception as exc:
        error_text = (
            f"❌ *Ошибка аудита*\n\n"
            f"`{type(exc).__name__}: {exc}`\n\n"
            f"Попробуйте изменить параметры или проверьте URL."
        )
        _schedule_message(application, chat_id, error_text, parse_mode="Markdown")
        _schedule_keyboard(application, chat_id, build_audit_done_keyboard())

        logger.error(
            "Ошибка аудита из бота",
            extra={"context": {
                "user_id": session.user_id,
                "url": params.base_url,
                "error": str(exc),
            }},
            exc_info=True,
        )

    finally:
        session.state = UserState.MENU


# ── Вспомогательные функции для отправки из потока ────────────

def _schedule_message(
    application: Application,  # type: ignore[type-arg]
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Планирует отправку сообщения из фонового потока."""
    async def _send(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
        )

    application.job_queue.run_once(_send, when=0)  # type: ignore[union-attr]


def _schedule_file(
    application: Application,  # type: ignore[type-arg]
    chat_id: int,
    file_path: str,
    caption: str,
) -> None:
    """Планирует отправку файла из фонового потока."""
    async def _send(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            with open(file_path, "rb") as f:
                await ctx.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                )
        except FileNotFoundError:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Файл не найден: {file_path}",
            )

    application.job_queue.run_once(_send, when=0)  # type: ignore[union-attr]


def _schedule_keyboard(
    application: Application,  # type: ignore[type-arg]
    chat_id: int,
    reply_markup: Any,
) -> None:
    """Планирует отправку сообщения с клавиатурой из фонового потока."""
    async def _send(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text="Выберите действие:",
            reply_markup=reply_markup,
        )

    application.job_queue.run_once(_send, when=0)  # type: ignore[union-attr]


# ── Регистрация обработчиков ──────────────────────────────────

def register_handlers(
    application: Application,  # type: ignore[type-arg]
    session_manager: SessionManager,
    audit_service: AuditService,
    settings: Settings,
) -> None:
    """
    Регистрирует все обработчики в приложении Telegram-бота.

    Args:
        application: экземпляр Application из python-telegram-bot.
        session_manager: менеджер пользовательских сессий.
        audit_service: сервис аудита.
        settings: настройки приложения.
    """
    application.bot_data["session_manager"] = session_manager
    application.bot_data["audit_service"] = audit_service
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Обработчики бота зарегистрированы")


async def _cb_cancel(
    query: Any,
    session: UserSession,
    session_manager: SessionManager,
) -> None:
    """Отмена и сброс сессии."""
    session_manager.reset(session.user_id)

    await _safe_edit_text(
        query,
        "👋 Сессия завершена.\n\n"
        "Нажмите /start, чтобы начать новый аудит.",
    )
