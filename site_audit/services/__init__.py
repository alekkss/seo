"""
Пакет сервисов приложения.

Содержит бизнес-логику, отделённую от точек входа (CLI, Telegram-бот).

Использование:
    from site_audit.services import AuditService
"""

from .audit_service import AuditService

__all__ = [
    "AuditService",
]
