"""Configuração de e-mail sem credenciais, carregada de JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class EmailSettings:
    mode: str = "dry-run"
    sender: str = ""
    allowed_recipient_domains: list[str] = field(default_factory=list)
    approved_recipient_groups: list[str] = field(default_factory=list)
    max_recipients: int = 10
    attach_full_html: bool = True
    report_base_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True


def load_settings(path: str | Path) -> EmailSettings:
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Configuração de e-mail não encontrada: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = set(EmailSettings.__dataclass_fields__)
    return EmailSettings(**{key: value for key, value in raw.items() if key in known})
