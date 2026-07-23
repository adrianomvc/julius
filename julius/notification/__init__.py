"""Notificação desacoplada: Relatório → Mensagem → Transporte."""

from julius.notification.config import EmailSettings, load_settings
from julius.notification.models import EmailMessage, SendResult
from julius.notification.policy import NotificationPolicy, PolicyDecision
from julius.notification.send_log import SendLog, SendRecord
from julius.notification.service import NotificationService

__all__ = [
    "EmailMessage",
    "EmailSettings",
    "NotificationPolicy",
    "NotificationService",
    "PolicyDecision",
    "SendLog",
    "SendRecord",
    "SendResult",
    "load_settings",
]
