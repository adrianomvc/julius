"""Cadastro local e explícito de destinatários por conta analisada."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class RecipientRegistryError(ValueError):
    """Cadastro ausente, ambíguo ou inválido."""


@dataclass(frozen=True)
class AccountRecipients:
    account: str
    to: list[str]
    cc: list[str]
    recipient_group: str
    enabled: bool = False


@dataclass(frozen=True)
class RecipientRegistry:
    accounts: dict[str, AccountRecipients]

    def for_account(self, account: str) -> AccountRecipients:
        recipients = self.accounts.get(account)
        if recipients is None:
            raise RecipientRegistryError(
                f"conta sem cadastro de destinatários: {account}"
            )
        if not recipients.enabled:
            raise RecipientRegistryError(
                f"envio desabilitado no cadastro da conta: {account}"
            )
        return recipients


def load_recipient_registry(path: str | Path) -> RecipientRegistry:
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(
            f"cadastro de destinatários não encontrado: {file_path}"
        )
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
        raise RecipientRegistryError("schema_version do cadastro deve ser 1.0")
    items = raw.get("accounts")
    if not isinstance(items, list):
        raise RecipientRegistryError("accounts deve ser uma lista")

    accounts: dict[str, AccountRecipients] = {}
    expected = {"account", "to", "cc", "recipient_group", "enabled"}
    for item in items:
        if not isinstance(item, dict) or set(item) != expected:
            raise RecipientRegistryError(
                "cadastro de conta possui campos ausentes ou não permitidos"
            )
        account = item["account"]
        to = item["to"]
        cc = item["cc"]
        group = item["recipient_group"]
        enabled = item["enabled"]
        if not isinstance(account, str) or not account.strip():
            raise RecipientRegistryError("identificador de conta inválido")
        if account in accounts:
            raise RecipientRegistryError(f"conta duplicada no cadastro: {account}")
        if not isinstance(to, list) or not isinstance(cc, list):
            raise RecipientRegistryError("to e cc devem ser listas")
        if not isinstance(group, str) or not group.strip():
            raise RecipientRegistryError("recipient_group inválido")
        if not isinstance(enabled, bool):
            raise RecipientRegistryError("enabled deve ser booleano")
        addresses = to + cc
        if enabled and not to:
            raise RecipientRegistryError(
                f"conta habilitada sem destinatário principal: {account}"
            )
        if not all(isinstance(address, str) and _valid_email(address) for address in addresses):
            raise RecipientRegistryError(f"e-mail inválido no cadastro da conta: {account}")
        normalized = [address.strip().lower() for address in addresses]
        if len(normalized) != len(set(normalized)):
            raise RecipientRegistryError(
                f"destinatário duplicado no cadastro da conta: {account}"
            )
        accounts[account] = AccountRecipients(
            account=account,
            to=[address.strip() for address in to],
            cc=[address.strip() for address in cc],
            recipient_group=group,
            enabled=enabled,
        )
    return RecipientRegistry(accounts)


def _valid_email(address: str) -> bool:
    value = address.strip()
    if value != address or value.count("@") != 1 or any(char.isspace() for char in value):
        return False
    local, domain = value.split("@", 1)
    return bool(local and domain and "." in domain and not domain.startswith("."))
