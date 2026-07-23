"""Transporte dry-run: compõe a mensagem numa outbox, sem enviar."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from julius.notification.models import EmailMessage, SendResult


class DryRunTransport:
    name = "dry_run"

    def __init__(self, outbox_root: str | Path, scan_id: str) -> None:
        self.dir = Path(outbox_root) / scan_id
        self.scan_id = scan_id

    def send(self, message: EmailMessage) -> SendResult:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "email.html").write_text(message.html_body, encoding="utf-8")
        (self.dir / "email.txt").write_text(message.text_body, encoding="utf-8")
        for name, content in message.attachments:
            (self.dir / name).write_bytes(content)
        manifest = {
            "mode": "dry-run",
            "subject": message.subject,
            "sender": message.sender,
            "recipients": message.recipients,
            "cc": message.cc,
            "scan_id": self.scan_id,
            "idempotency_key": message.idempotency_key,
            "tags": message.tags,
            "created_at": datetime.now().astimezone().isoformat(),
            "status": "composed_not_sent",
        }
        (self.dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return SendResult(
            status="composed_not_sent", transport=self.name, outbox_dir=str(self.dir)
        )
