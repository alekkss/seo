"""
Хранение состояния пользовательских сессий Telegram-бота.

Каждый пользователь имеет изолированную сессию с параметрами аудита.
Состояние хранится в памяти (dict), сбрасывается при перезапуске бота.

Использование:
    from site_audit.bot.states import SessionManager

    manager = SessionManager()
    session = manager.get(user_id=123456)
    session.url = "https://example.com"
    session.toggle_check("seo")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from site_audit.services.audit_service import ALL_CHECKS


class UserState(Enum):
    """Этапы взаимодействия пользователя с ботом."""

    IDLE = auto()
    WAITING_URL = auto()
    MENU = auto()
    SELECTING_CHECKS = auto()
    SETTINGS = auto()
    WAITING_SETTING_VALUE = auto()
    AUDIT_RUNNING = auto()


# ── Настраиваемые параметры с метаданными ─────────────────────

@dataclass(frozen=True)
class SettingMeta:
    """Описание одного настраиваемого параметра аудита."""

    key: str
    label: str
    value_type: type
    min_value: int | float | None = None
    max_value: int | float | None = None
    description: str = ""


EDITABLE_SETTINGS: list[SettingMeta] = [
    SettingMeta(
        key="limit",
        label="Лимит страниц",
        value_type=int,
        min_value=0,
        max_value=10000,
        description="Максимальное количество URL для проверки (0 = без лимита)",
    ),
    SettingMeta(
        key="workers",
        label="Потоки",
        value_type=int,
        min_value=1,
        max_value=50,
        description="Количество параллельных потоков загрузки",
    ),
    SettingMeta(
        key="max_depth",
        label="Глубина обхода",
        value_type=int,
        min_value=1,
        max_value=10,
        description="Максимальная глубина BFS-обхода",
    ),
    SettingMeta(
        key="timeout",
        label="Таймаут (сек)",
        value_type=int,
        min_value=1,
        max_value=120,
        description="Таймаут HTTP-запросов в секундах",
    ),
    SettingMeta(
        key="delay",
        label="Задержка (сек)",
        value_type=float,
        min_value=0.0,
        max_value=30.0,
        description="Задержка между запросами в секундах",
    ),
    SettingMeta(
        key="max_image_size_kb",
        label="Макс. размер картинки (КБ)",
        value_type=int,
        min_value=50,
        max_value=10000,
        description="Порог тяжёлой картинки в килобайтах",
    ),
    SettingMeta(
        key="min_text_length",
        label="Мин. длина текста",
        value_type=int,
        min_value=0,
        max_value=5000,
        description="Порог пустой страницы (символов видимого текста)",
    ),
]

SETTINGS_BY_KEY: dict[str, SettingMeta] = {s.key: s for s in EDITABLE_SETTINGS}


# ── Сессия пользователя ───────────────────────────────────────

@dataclass
class UserSession:
    """Состояние одного пользователя."""

    user_id: int
    state: UserState = UserState.IDLE
    url: str = ""

    # Выбранные проверки (по умолчанию — все включены)
    selected_checks: dict[str, bool] = field(default_factory=dict)

    # Параметры аудита
    limit: int = 0
    workers: int = 10
    max_depth: int = 3
    timeout: int = 15
    delay: float = 0.0
    max_image_size_kb: int = 500
    min_text_length: int = 100
    check_external_links: bool = False

    # Контекст для ожидания ввода значения настройки
    pending_setting_key: str = ""

    def __post_init__(self) -> None:
        """Инициализирует все проверки как выбранные."""
        if not self.selected_checks:
            self.selected_checks = {name: True for name in ALL_CHECKS}

    def toggle_check(self, check_name: str) -> bool:
        """
        Переключает состояние проверки (вкл/выкл).

        Args:
            check_name: имя проверки.

        Returns:
            Новое состояние проверки (True = включена).
        """
        if check_name not in self.selected_checks:
            return False
        self.selected_checks[check_name] = not self.selected_checks[check_name]
        return self.selected_checks[check_name]

    def get_enabled_checks(self) -> list[str]:
        """Возвращает список включённых проверок."""
        return [name for name, enabled in self.selected_checks.items() if enabled]

    def get_setting_value(self, key: str) -> Any:
        """
        Возвращает текущее значение параметра по ключу.

        Args:
            key: ключ параметра (например, 'limit', 'workers').

        Returns:
            Текущее значение или None, если ключ не найден.
        """
        return getattr(self, key, None)

    def set_setting_value(self, key: str, raw_value: str) -> str | None:
        """
        Устанавливает значение параметра из строки.

        Args:
            key: ключ параметра.
            raw_value: строковое значение от пользователя.

        Returns:
            Сообщение об ошибке или None при успехе.
        """
        meta = SETTINGS_BY_KEY.get(key)
        if meta is None:
            return f"Неизвестный параметр: {key}"

        try:
            value = meta.value_type(raw_value.strip())
        except (ValueError, TypeError):
            type_name = "целое число" if meta.value_type is int else "число"
            return f"Некорректное значение. Ожидается {type_name}."

        if meta.min_value is not None and value < meta.min_value:
            return f"Значение не может быть меньше {meta.min_value}."

        if meta.max_value is not None and value > meta.max_value:
            return f"Значение не может быть больше {meta.max_value}."

        setattr(self, key, value)
        return None

    def reset(self) -> None:
        """Сбрасывает сессию в начальное состояние."""
        self.state = UserState.IDLE
        self.url = ""
        self.selected_checks = {name: True for name in ALL_CHECKS}
        self.limit = 0
        self.workers = 10
        self.max_depth = 3
        self.timeout = 15
        self.delay = 0.0
        self.max_image_size_kb = 500
        self.min_text_length = 100
        self.check_external_links = False
        self.pending_setting_key = ""


# ── Менеджер сессий ───────────────────────────────────────────

class SessionManager:
    """
    Управляет сессиями всех пользователей бота.

    Хранит состояние в памяти. При перезапуске бота сессии сбрасываются.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, UserSession] = {}

    def get(self, user_id: int) -> UserSession:
        """
        Возвращает сессию пользователя. Создаёт новую, если не существует.

        Args:
            user_id: Telegram ID пользователя.

        Returns:
            Объект UserSession для данного пользователя.
        """
        if user_id not in self._sessions:
            self._sessions[user_id] = UserSession(user_id=user_id)
        return self._sessions[user_id]

    def reset(self, user_id: int) -> None:
        """
        Сбрасывает сессию пользователя.

        Args:
            user_id: Telegram ID пользователя.
        """
        if user_id in self._sessions:
            self._sessions[user_id].reset()

    def remove(self, user_id: int) -> None:
        """
        Удаляет сессию пользователя полностью.

        Args:
            user_id: Telegram ID пользователя.
        """
        self._sessions.pop(user_id, None)
