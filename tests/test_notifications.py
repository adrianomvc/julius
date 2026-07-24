"""Notificações do MVP 4: guardrails, idempotência e transportes offline."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from julius.cli import app
from julius.notification import (
    EmailMessage,
    EmailSettings,
    NotificationPolicy,
    NotificationService,
    RecipientRegistryError,
    SendLog,
    SendResult,
    load_recipient_registry,
    load_settings,
)
from julius.notification.transports import DryRunTransport, SmtpTransport


def _settings(**overrides) -> EmailSettings:
    values = {
        "mode": "active",
        "sender": "julius@empresa.com",
        "allowed_recipient_domains": ["empresa.com"],
        "approved_recipient_groups": ["account-owners"],
    }
    values.update(overrides)
    return EmailSettings(**values)


def _message() -> EmailMessage:
    return EmailMessage(
        subject="Julius · plano de ação",
        sender="julius@empresa.com",
        recipients=["squad@empresa.com"],
        text_body="Resumo em texto",
        html_body="<strong>Resumo</strong>",
        attachments=[("report.html", b"<html>relatorio</html>")],
        tags={"scan_id": "scan-1"},
        idempotency_key="scan-1|account-report|account-owners",
    )


def test_dry_run_writes_outbox_and_idempotency_manifest(tmp_path):
    service = NotificationService(DryRunTransport(tmp_path, "scan-1"))
    result = service.send_report(
        subject="Plano",
        sender="julius@empresa.com",
        recipients=["squad@empresa.com"],
        html_body="<p>Plano</p>",
        text_body="Plano",
        scan_id="scan-1",
        report_html="<html>Relatório</html>",
    )

    manifest = json.loads(
        (tmp_path / "scan-1" / "manifest.json").read_text(encoding="utf-8")
    )
    assert result.status == "composed_not_sent"
    assert manifest["idempotency_key"] == "scan-1|account-report|account-owners"
    assert (tmp_path / "scan-1" / "report.html").exists()


def test_active_policy_requires_confirmation_and_allowlist():
    policy_guard = NotificationPolicy(_settings())
    message = _message()

    denied = policy_guard.evaluate(
        message,
        mode="active",
        confirmed=False,
        non_interactive=False,
        recipient_group="account-owners",
    )
    assert not denied.allowed
    assert "confirmação" in denied.reason

    message.recipients = ["externo@example.net"]
    denied = policy_guard.evaluate(
        message,
        mode="active",
        confirmed=True,
        non_interactive=False,
        recipient_group="account-owners",
    )
    assert not denied.allowed
    assert "allowlist" in denied.reason


def test_non_interactive_requires_preapproved_group():
    policy_guard = NotificationPolicy(_settings())
    denied = policy_guard.evaluate(
        _message(),
        mode="active",
        confirmed=False,
        non_interactive=True,
        recipient_group="finance",
    )
    allowed = policy_guard.evaluate(
        _message(),
        mode="active",
        confirmed=False,
        non_interactive=True,
        recipient_group="account-owners",
    )
    assert not denied.allowed
    assert allowed.allowed


def test_service_blocks_duplicate_active_send(tmp_path):
    class FakeTransport:
        name = "smtp"

        def __init__(self):
            self.calls = 0

        def send(self, message):
            self.calls += 1
            return SendResult(
                status="sent",
                transport=self.name,
                provider_message_id="provider-1",
            )

    transport = FakeTransport()
    service = NotificationService(
        transport,
        policy=NotificationPolicy(_settings()),
        send_log=SendLog(tmp_path / "send-log.json"),
    )
    kwargs = {
        "subject": "Plano",
        "sender": "julius@empresa.com",
        "recipients": ["squad@empresa.com"],
        "html_body": "<p>Plano</p>",
        "text_body": "Plano",
        "scan_id": "scan-1",
        "mode": "active",
        "confirmed": True,
    }

    first = service.send_report(**kwargs)
    duplicate = service.send_report(**kwargs)

    assert first.status == "sent"
    assert duplicate.status == "blocked"
    assert "já enviada" in duplicate.reason
    assert transport.calls == 1


def test_smtp_transport_uses_tls_and_environment_credentials():
    events = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send", message["Subject"]))
            return {}

    result = SmtpTransport(
        "smtp.empresa.com",
        username="julius",
        password="secret-from-env",
        client_factory=FakeSmtp,
    ).send(_message())

    assert result.status == "sent"
    assert ("starttls",) in events
    assert ("login", "julius", "secret-from-env") in events
    assert ("send", "Julius · plano de ação") in events


def test_email_config_contains_no_credentials(tmp_path):
    path = tmp_path / "email.json"
    path.write_text(
        json.dumps(
            {
                "mode": "dry-run",
                "sender": "julius@empresa.com",
                "smtp_password": "must-be-ignored",
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(path)
    assert settings.mode == "dry-run"
    assert not hasattr(settings, "smtp_password")
    assert not hasattr(settings, "aws_region")
    assert not hasattr(settings, "transport")


def test_recipient_registry_resolves_exact_enabled_account(tmp_path):
    path = tmp_path / "recipients.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "accounts": [
                    {
                        "account": "consumer-avi",
                        "to": ["squad-avi@empresa.com"],
                        "cc": ["finops@empresa.com"],
                        "recipient_group": "account-owners",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    recipients = load_recipient_registry(path).for_account("consumer-avi")
    assert recipients.to == ["squad-avi@empresa.com"]
    assert recipients.cc == ["finops@empresa.com"]
    with pytest.raises(RecipientRegistryError, match="sem cadastro"):
        load_recipient_registry(path).for_account("outra-conta")


def test_recipient_registry_blocks_disabled_and_duplicate_addresses(tmp_path):
    disabled = tmp_path / "disabled.json"
    disabled.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "accounts": [
                    {
                        "account": "consumer-avi",
                        "to": ["squad@empresa.com"],
                        "cc": [],
                        "recipient_group": "account-owners",
                        "enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RecipientRegistryError, match="desabilitado"):
        load_recipient_registry(disabled).for_account("consumer-avi")

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "accounts": [
                    {
                        "account": "consumer-avi",
                        "to": ["squad@empresa.com"],
                        "cc": ["SQUAD@empresa.com"],
                        "recipient_group": "account-owners",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RecipientRegistryError, match="duplicado"):
        load_recipient_registry(duplicate)


def test_active_cli_rejects_manual_recipient_before_transport_access():
    result = CliRunner().invoke(
        app,
        [
            "notify",
            "--mode",
            "active",
            "--to",
            "manual@empresa.com",
        ],
    )
    assert result.exit_code != 0
    assert "--to não é permitido" in result.output


def test_notify_cli_defaults_to_offline_dry_run(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "notify",
            "--outbox",
            str(tmp_path),
            "--to",
            "squad@empresa.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "composed_not_sent" in result.output
    manifests = list(tmp_path.glob("*/manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["mode"] == "dry-run"


def test_notify_cli_has_no_aws_email_transport_options():
    result = CliRunner().invoke(app, ["notify", "--help"])

    assert result.exit_code == 0
    assert "--transport" not in result.output
    assert "--profile" not in result.output
    assert "--region" not in result.output
