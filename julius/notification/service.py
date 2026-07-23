"""Serviço de notificação: monta a EmailMessage e delega ao transporte."""

from __future__ import annotations

from julius.notification.models import EmailMessage, SendResult
from julius.notification.policy import NotificationPolicy
from julius.notification.send_log import SendLog
from julius.notification.transports.base import EmailTransport


class NotificationService:
    def __init__(
        self,
        transport: EmailTransport,
        *,
        policy: NotificationPolicy | None = None,
        send_log: SendLog | None = None,
    ) -> None:
        self.transport = transport
        self.policy = policy
        self.send_log = send_log

    def send_report(
        self,
        *,
        subject: str,
        sender: str,
        recipients: list[str],
        cc: list[str] | None = None,
        html_body: str,
        text_body: str,
        scan_id: str,
        report_html: str | None = None,
        recipient_group: str = "account-owners",
        mode: str = "dry-run",
        confirmed: bool = False,
        non_interactive: bool = False,
        critical_error: bool = False,
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
            cc=cc or [],
            attachments=attachments,
            tags={"scan_id": scan_id, "report_type": "account-report"},
            idempotency_key=f"{scan_id}|account-report|{recipient_group}",
        )
        if mode not in {"dry-run", "active"}:
            return SendResult(
                status="blocked",
                transport=self.transport.name,
                reason=f"modo de envio inválido: {mode}",
            )
        if mode == "dry-run":
            if self.transport.name != "dry_run":
                return SendResult(
                    status="blocked",
                    transport=self.transport.name,
                    reason="dry-run exige o transporte dry_run",
                )
            return self.transport.send(message)

        if self.transport.name == "dry_run":
            return SendResult(
                status="blocked",
                transport=self.transport.name,
                reason="envio ativo não pode usar o transporte dry_run",
            )
        if self.policy is None:
            return SendResult(
                status="blocked",
                transport=self.transport.name,
                reason="política de envio ativo ausente",
            )
        if self.send_log is None:
            return SendResult(
                status="blocked",
                transport=self.transport.name,
                reason="log idempotente de envio ativo ausente",
            )
        decision = self.policy.evaluate(
            message,
            mode=mode,
            confirmed=confirmed,
            non_interactive=non_interactive,
            recipient_group=recipient_group,
            critical_error=critical_error,
        )
        if not decision.allowed:
            return SendResult(
                status="blocked",
                transport=self.transport.name,
                reason=decision.reason,
            )
        if self.send_log.was_sent(message.idempotency_key):
            return SendResult(
                status="blocked",
                transport=self.transport.name,
                reason="mensagem já enviada para este scan e grupo",
            )
        result = self.transport.send(message)
        if result.status == "sent":
            self.send_log.record(message.idempotency_key, result)
        return result
