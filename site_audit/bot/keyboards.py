"""
Inline-клавиатуры Telegram-бота.

Все клавиатуры генерируются динамически на основе текущего состояния
сессии пользователя. Callback-данные используют префиксы для маршрутизации:
    menu:*       — главное меню
    check:*      — выбор проверок
    setting:*    — настройки параметров
    audit:*      — управление аудитом

Использование:
    from site_audit.bot.keyboards import build_main_menu
    from site_audit.bot.states import UserSession

    session = UserSession(user_id=123)
    session.url = "https://example.com"
    markup = build_main_menu(session)
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from site_audit.services.audit_service import ALL_CHECKS

from .states import EDITABLE_SETTINGS, UserSession


# ── Callback-префиксы ────────────────────────────────────────

PREFIX_MENU = "menu"
PREFIX_CHECK = "check"
PREFIX_SETTING = "setting"
PREFIX_AUDIT = "audit"

# ── Callback-данные ───────────────────────────────────────────

CB_NEW_AUDIT = f"{PREFIX_MENU}:new_audit"
CB_SELECT_CHECKS = f"{PREFIX_MENU}:select_checks"
CB_SETTINGS = f"{PREFIX_MENU}:settings"
CB_RUN_AUDIT = f"{PREFIX_AUDIT}:run"
CB_CANCEL = f"{PREFIX_MENU}:cancel"
CB_CHECKS_ALL_ON = f"{PREFIX_CHECK}:all_on"
CB_CHECKS_ALL_OFF = f"{PREFIX_CHECK}:all_off"
CB_CHECKS_BACK = f"{PREFIX_CHECK}:back"
CB_SETTINGS_BACK = f"{PREFIX_SETTING}:back"
CB_SETTINGS_EXTERNAL = f"{PREFIX_SETTING}:external_links"


def build_start_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура начального экрана (/start).

    Returns:
        Клавиатура с кнопкой «Начать аудит».
    """
    keyboard = [
        [InlineKeyboardButton("🔍 Начать аудит", callback_data=CB_NEW_AUDIT)],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_main_menu(session: UserSession) -> InlineKeyboardMarkup:
    """
    Главное меню после ввода URL.

    Показывает текущий URL, количество выбранных проверок
    и кнопки управления.

    Args:
        session: текущая сессия пользователя.

    Returns:
        Клавиатура главного меню.
    """
    enabled_count = len(session.get_enabled_checks())
    total_count = len(ALL_CHECKS)

    keyboard = [
        [
            InlineKeyboardButton(
                f"☑️ Проверки ({enabled_count}/{total_count})",
                callback_data=CB_SELECT_CHECKS,
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ Настройки",
                callback_data=CB_SETTINGS,
            ),
        ],
        [
            InlineKeyboardButton(
                "🚀 Запустить аудит",
                callback_data=CB_RUN_AUDIT,
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Отмена",
                callback_data=CB_CANCEL,
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_checks_keyboard(session: UserSession) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора проверок с toggle-кнопками.

    Каждая проверка показывается с индикатором ✅ (вкл) или ⬜ (выкл).
    Внизу — кнопки «Включить все», «Выключить все» и «Назад».

    Args:
        session: текущая сессия пользователя.

    Returns:
        Клавиатура выбора проверок.
    """
    keyboard: list[list[InlineKeyboardButton]] = []

    for name, entry in ALL_CHECKS.items():
        is_enabled = session.selected_checks.get(name, True)
        icon = "✅" if is_enabled else "⬜"
        label = entry["description"]

        keyboard.append([
            InlineKeyboardButton(
                f"{icon} {label}",
                callback_data=f"{PREFIX_CHECK}:{name}",
            ),
        ])

    # Управляющие кнопки
    keyboard.append([
        InlineKeyboardButton("✅ Все вкл", callback_data=CB_CHECKS_ALL_ON),
        InlineKeyboardButton("⬜ Все выкл", callback_data=CB_CHECKS_ALL_OFF),
    ])
    keyboard.append([
        InlineKeyboardButton("◀️ Назад", callback_data=CB_CHECKS_BACK),
    ])

    return InlineKeyboardMarkup(keyboard)


def build_settings_keyboard(session: UserSession) -> InlineKeyboardMarkup:
    """
    Клавиатура настроек параметров аудита.

    Каждый параметр показывает текущее значение.
    Отдельная кнопка для toggle внешних ссылок.

    Args:
        session: текущая сессия пользователя.

    Returns:
        Клавиатура настроек.
    """
    keyboard: list[list[InlineKeyboardButton]] = []

    for meta in EDITABLE_SETTINGS:
        current_value = session.get_setting_value(meta.key)
        label = f"{meta.label}: {current_value}"

        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=f"{PREFIX_SETTING}:{meta.key}",
            ),
        ])

    # Toggle внешних ссылок
    ext_icon = "✅" if session.check_external_links else "⬜"
    keyboard.append([
        InlineKeyboardButton(
            f"{ext_icon} Проверять внешние ссылки",
            callback_data=CB_SETTINGS_EXTERNAL,
        ),
    ])

    keyboard.append([
        InlineKeyboardButton("◀️ Назад", callback_data=CB_SETTINGS_BACK),
    ])

    return InlineKeyboardMarkup(keyboard)


def build_setting_input_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура при ожидании ввода значения настройки.

    Returns:
        Клавиатура с кнопкой «Отмена».
    """
    keyboard = [
        [InlineKeyboardButton("◀️ Отмена", callback_data=CB_SETTINGS_BACK)],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_audit_running_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура во время выполнения аудита.

    Returns:
        Клавиатура с кнопкой-заглушкой (аудит нельзя прервать).
    """
    keyboard = [
        [InlineKeyboardButton("⏳ Аудит выполняется...", callback_data="noop")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_audit_done_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура после завершения аудита.

    Returns:
        Клавиатура с кнопками «Новый аудит» и «Завершить».
    """
    keyboard = [
        [InlineKeyboardButton("🔍 Новый аудит", callback_data=CB_NEW_AUDIT)],
        [InlineKeyboardButton("❌ Завершить", callback_data=CB_CANCEL)],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_session_summary(session: UserSession) -> str:
    """
    Формирует текстовое описание текущих параметров сессии.

    Args:
        session: текущая сессия пользователя.

    Returns:
        Форматированная строка с параметрами.
    """
    enabled_checks = session.get_enabled_checks()
    checks_list = "\n".join(
        f"  ✅ {ALL_CHECKS[name]['description']}" for name in enabled_checks
    )

    if not checks_list:
        checks_list = "  ⚠️ Ни одна проверка не выбрана!"

    ext_status = "да" if session.check_external_links else "нет"

    return (
        f"🌐 *URL:* `{session.url}`\n"
        f"\n"
        f"📋 *Проверки:*\n"
        f"{checks_list}\n"
        f"\n"
        f"⚙️ *Параметры:*\n"
        f"  Лимит страниц: {session.limit} {'(без лимита)' if session.limit == 0 else ''}\n"
        f"  Потоки: {session.workers}\n"
        f"  Глубина обхода: {session.max_depth}\n"
        f"  Таймаут: {session.timeout} сек\n"
        f"  Задержка: {session.delay} сек\n"
        f"  Макс. размер картинки: {session.max_image_size_kb} КБ\n"
        f"  Мин. длина текста: {session.min_text_length}\n"
        f"  Внешние ссылки: {ext_status}\n"
    )
