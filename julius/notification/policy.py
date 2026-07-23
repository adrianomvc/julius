"""Guardrails obrigatórios antes de qualquer envio ativo."""

from __future__ import annotations

from dataclasses import dataclass

from julius.notification.config import EmailSettings
from julius.notification.models import EmailMessage


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


class NotificationPolicy:
    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings

    def evaluate(
        self,
        message: EmailMessage,
        *,
        mode: str,
        confirmed: bool,
        non_interactive: bool,
        recipient_group: str,
        critical_error: bool = False,
    ) -> PolicyDecision:
        if mode != "active":
            return PolicyDecision(True)
        if self.settings.mode != "active":
            return PolicyDecision(False, "configuração não autoriza mode=active")
        if not message.html_body.strip():
            return PolicyDecision(False, "HTML do e-mail não foi gerado")
        if critical_error:
            return PolicyDecision(False, "scan possui erro crítico")
        if not self.settings.sender or message.sender.lower() != self.settings.sender.lower():
            return PolicyDecision(False, "remetente não autorizado")

        recipients = message.recipients + message.cc
        if not recipients:
            return PolicyDecision(False, "nenhum destinatário")
        if len(recipients) > self.settings.max_recipients:
            return PolicyDecision(False, "limite de destinatários excedido")
        allowed_domains = {
            domain.lower().lstrip("@")
            for domain in self.settings.allowed_recipient_domains
        }
        if not allowed_domains:
            return PolicyDecision(False, "allowlist de domínios vazia")
        for address in recipients:
            domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
            if domain not in allowed_domains:
                return PolicyDecision(
                    False, f"destinatário fora da allowlist: {address}"
                )

        if non_interactive:
            if recipient_group not in self.settings.approved_recipient_groups:
                return PolicyDecision(
                    False, "grupo não aprovado para envio não interativo"
                )
        elif not confirmed:
            return PolicyDecision(False, "envio manual exige confirmação humana")
        return PolicyDecision(True)
