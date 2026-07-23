"""Serviço de notificação: monta a EmailMessage e delega ao transporte."""

from __future__ import annotations

from julius.notification.models import EmailMessage, SendResult
from julius.notification.transports.base import EmailTransport


class NotificationService:
    def __init__(self, transport: EmailTransport) -> None:
        self.transport = transport

    def send_report(
        self,
        *,
        subject: str,
        sender: str,
        recipients: list[str],
        html_body: str,
        text_body: str,
        scan_id: str,
        report_html: str | None = None,
        recipient_group: str = "account-owners",
    ) -> SendResult:
        attachments: list[tuple[str, bytes]] = []
        if report_html is not None:
            attachments.append(("report.html", report_html.encode("utf-8")))
        message = EmailMessage(
            subject=subject,
            sender=sender,
            recipients=recipients,
            text_body=text_body,
            html_body=html_body,
            attachments=attachments,
            tags={"scan_id": scan_id, "report_type": "account-report"},
            idempotency_key=f"{scan_id}|account-report|{recipient_group}",
        )
        return self.transport.send(message)
