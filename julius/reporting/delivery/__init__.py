"""Notificação desacoplada: Relatório → Mensagem → Transporte."""

from julius.reporting.delivery.config import EmailSettings, load_settings
from julius.reporting.delivery.models import EmailMessage, SendResult
from julius.reporting.delivery.policy import NotificationPolicy, PolicyDecision
from julius.reporting.delivery.recipients import (
    AccountRecipients,
    RecipientRegistry,
    RecipientRegistryError,
    load_recipient_registry,
)
from julius.reporting.delivery.send_log import SendLog, SendRecord
from julius.reporting.delivery.service import NotificationService

__all__ = [
    "EmailMessage",
    "EmailSettings",
    "AccountRecipients",
    "NotificationPolicy",
    "NotificationService",
    "PolicyDecision",
    "RecipientRegistry",
    "RecipientRegistryError",
    "SendLog",
    "SendRecord",
    "SendResult",
    "load_settings",
    "load_recipient_registry",
]
