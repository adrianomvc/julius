"""Composição e envio do relatório por e-mail."""

from __future__ import annotations

import json
import os
import webbrowser
from pathlib import Path

import typer

from julius.cli._shared import (
    _DEFAULT_INPUT,
    _load_agent_enrichment,
    _require_same_opportunity_set,
    app,
)
from julius.collection.normalizers import load_account
from julius.pipeline import analyze
from julius.reporting import renderer
from julius.reporting.contextual import attach_contextual_analysis
from julius.reporting.delivery import (
    NotificationPolicy,
    NotificationService,
    RecipientRegistryError,
    SendLog,
    load_recipient_registry,
    load_settings,
)
from julius.reporting.delivery.transports import DryRunTransport, SmtpTransport


@app.command()
def notify(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    mode: str = typer.Option("dry-run", "--mode", help="dry-run | active"),
    outbox: str = typer.Option("data/outbox", "--outbox"),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only usado na análise de código."
    ),
    to: str = typer.Option(
        "", "--to", help="Destinatários de prévia separados por vírgula (somente dry-run)."
    ),
    report_url: str = typer.Option(
        "", "--report-url", help="URL hospedada do relatório; vazio = anexo (report.html)."
    ),
    open_preview: bool = typer.Option(False, "--open-preview"),
    email_config: str = typer.Option("~/.julius-email.json", "--email-config"),
    recipients_config: str = typer.Option(
        "~/.julius-recipients.json",
        "--recipients-config",
        help="Cadastro local de destinatários por conta.",
    ),
    send_log: str = typer.Option("data/state/send_log.json", "--send-log"),
    recipient_group: str = typer.Option(
        "", "--recipient-group", help="Grupo da prévia (somente dry-run)."
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="Confirmação humana obrigatória para envio ativo manual."
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Somente para grupos previamente aprovados na configuração.",
    ),
    agent_context: str = typer.Option("", "--agent-context"),
    agent_result: str = typer.Option("", "--agent-result"),
) -> None:
    """Compõe em outbox por padrão; envio ativo exige configuração e guardrails."""
    if mode not in {"dry-run", "active"}:
        raise typer.BadParameter("--mode deve ser dry-run ou active.")
    context, contextual = _load_agent_enrichment(agent_context, agent_result)
    if context and str(context.account["id"]) != load_account(input).account_id:
        raise typer.BadParameter("contexto Devin pertence a outra conta.")
    a = analyze(
        input,
        scan_id=context.scan_id if context else None,
        artifacts_manifest=artifacts_manifest or None,
    )
    _require_same_opportunity_set(context, a)
    if context:
        attach_contextual_analysis(a.vm, contextual)
    settings = None
    registered = None
    if mode == "active":
        if to:
            raise typer.BadParameter(
                "--to não é permitido em active; use o cadastro por conta."
            )
        if recipient_group:
            raise typer.BadParameter(
                "--recipient-group não é permitido em active; use o cadastro por conta."
            )
        try:
            settings = load_settings(email_config)
            registered = load_recipient_registry(
                recipients_config
            ).for_account(a.account.account_id)
        except (
            FileNotFoundError,
            RecipientRegistryError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise typer.BadParameter(str(exc)) from exc

    effective_url = report_url or (settings.report_base_url if settings else "")
    html, text = renderer.render_email(a.vm, report_url=effective_url or None)
    attach_report = settings.attach_full_html if settings else True
    report_html = renderer.render_html(a.vm) if attach_report or not effective_url else None
    recipients = [
        address.strip()
        for address in to.split(",")
        if address.strip()
    ]

    if mode == "dry-run":
        recipients = recipients or ["squad@empresa.com"]
        cc: list[str] = []
        effective_group = recipient_group or "account-owners"
        sender = "julius@empresa.com"
        service = NotificationService(DryRunTransport(outbox, a.scan_id))
    else:
        assert settings is not None and registered is not None
        recipients = registered.to
        cc = registered.cc
        effective_group = registered.recipient_group
        sender = settings.sender
        active_transport = SmtpTransport(
            settings.smtp_host,
            port=settings.smtp_port,
            username=os.getenv("JULIUS_SMTP_USERNAME", ""),
            password=os.getenv("JULIUS_SMTP_PASSWORD", ""),
            starttls=settings.smtp_starttls,
        )
        service = NotificationService(
            active_transport,
            policy=NotificationPolicy(settings),
            send_log=SendLog(send_log),
        )

    result = service.send_report(
        subject=renderer.subject(a.vm),
        sender=sender,
        recipients=recipients,
        cc=cc,
        html_body=html,
        text_body=text,
        scan_id=a.scan_id,
        report_html=report_html,
        recipient_group=effective_group,
        mode=mode,
        confirmed=confirm,
        non_interactive=non_interactive,
    )
    destination = result.outbox_dir or result.provider_message_id or result.reason or "-"
    typer.echo(f"{result.status} -> {destination}")
    typer.echo(f"Assunto: {renderer.subject(a.vm)}")
    if result.status == "blocked":
        raise typer.Exit(code=2)
    if open_preview and result.outbox_dir:
        webbrowser.open((Path(result.outbox_dir) / "email.html").resolve().as_uri())
