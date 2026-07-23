"""Notificação desacoplada: Relatório → Mensagem → Transporte."""

from julius.notification.models import EmailMessage, SendResult
from julius.notification.service import NotificationService

__all__ = ["EmailMessage", "NotificationService", "SendResult"]
