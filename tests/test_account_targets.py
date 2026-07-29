"""Escopo explícito e verificação STS da conta SSO ativa."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from julius.cli import app
from julius.collection.targets import (
    AccountTargetError,
    load_account_targets,
    verify_account_targets,
    write_verified_accounts,
)


class FakeSts:
    def __init__(self, account_id):
        self.account_id = account_id

    def get_caller_identity(self):
        return {
            "Account": self.account_id,
            "Arn": f"arn:aws:sts::{self.account_id}:assumed-role/JuliusReadOnly/test",
        }


class FakeSession:
    def __init__(self, account_id):
        self.account_id = account_id

    def client(self, name):
        assert name == "sts"
        return FakeSts(self.account_id)


def _write_config(path, accounts):
    path.write_text(
        json.dumps({"schema_version": "1.0", "accounts": accounts}),
        encoding="utf-8",
    )


def _target(name, account_id, sso_profile, *, enabled=True):
    return {
        "name": name,
        "expected_account_id": account_id,
        "sso_profile": sso_profile,
        "enabled": enabled,
    }


def test_loads_only_explicitly_enabled_accounts(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("principal", "123456789012", "principal"),
            _target("desabilitada", "999999999999", "disabled", enabled=False),
        ],
    )

    targets = load_account_targets(path)
    assert [target.name for target in targets] == ["principal"]


def test_verifies_each_sso_profile_in_sa_east_1(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("principal", "123456789012", "principal"),
            _target("secundaria", "210987654321", "secundaria"),
        ],
    )
    session_args = []
    accounts = {
        "principal": "123456789012",
        "secundaria": "210987654321",
    }

    verified, falhas = verify_account_targets(
        load_account_targets(path),
        session_factory=lambda profile, region: (
            session_args.append((profile, region)) or FakeSession(accounts[profile])
        ),
    )
    manifest = write_verified_accounts(verified, tmp_path / "verified.json")
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert falhas == []
    assert [account.account_id for account in verified] == [
        "123456789012",
        "210987654321",
    ]
    assert verified[0].region == "sa-east-1"
    assert verified[0].credential_source == "aws_cli_sso_profile"
    assert session_args == [
        ("principal", "sa-east-1"),
        ("secundaria", "sa-east-1"),
    ]
    assert payload["read_only"] is True
    assert payload["verified_with"] == "sts:GetCallerIdentity"


def test_stops_on_active_sso_account_mismatch(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(path, [_target("a", "123456789012", "principal")])

    with pytest.raises(AccountTargetError, match="esperado"):
        verify_account_targets(
            load_account_targets(path),
            session_factory=lambda *_: FakeSession("999999999999"),
        )


def test_an_expired_profile_does_not_take_the_other_accounts_down(tmp_path):
    """Um login SSO vencido é o caso comum de quem roda multi-conta.

    Antes ele abortava a verificação inteira, inclusive das contas cujo login
    estava válido — e o operador só descobria qual perfil renovar depois de
    testar um a um.
    """
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("expirada", "123456789012", "expirada"),
            _target("valida", "210987654321", "valida"),
        ],
    )

    def sessao(profile, _region):
        if profile == "expirada":
            raise RuntimeError("Token has expired and refresh failed")
        return FakeSession("210987654321")

    verified, falhas = verify_account_targets(
        load_account_targets(path), session_factory=sessao
    )

    assert [account.name for account in verified] == ["valida"]
    assert falhas == ["expirada: RuntimeError"]


def test_a_mismatch_still_stops_everything_even_next_to_a_failure(tmp_path):
    """Perfil que não responde é para ignorar; conta trocada nunca é."""
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("quebrada", "123456789012", "quebrada"),
            _target("trocada", "210987654321", "trocada"),
        ],
    )

    def sessao(profile, _region):
        if profile == "quebrada":
            raise RuntimeError("sem credencial")
        return FakeSession("999999999999")

    with pytest.raises(AccountTargetError, match="esperado"):
        verify_account_targets(load_account_targets(path), session_factory=sessao)


def test_rejects_implicit_or_ambiguous_scope(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("a", "123456789012", "a", enabled=False),
            _target("b", "210987654321", "b", enabled=False),
        ],
    )
    with pytest.raises(AccountTargetError, match="nenhuma conta"):
        load_account_targets(path)


def test_rejects_duplicate_sso_profile(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("a", "123456789012", "shared"),
            _target("b", "210987654321", "shared"),
        ],
    )

    with pytest.raises(AccountTargetError, match="perfil SSO"):
        load_account_targets(path)


def test_rejects_credentials_and_connection_fields_in_account_file(tmp_path):
    path = tmp_path / "accounts.json"
    account = _target("a", "123456789012", "principal")
    account["aws_access_key_id"] = "must-not-be-stored"
    _write_config(path, [account])

    with pytest.raises(AccountTargetError, match="não permitidos"):
        load_account_targets(path)


def test_collect_cli_exposes_only_sso_profile_and_fixed_region():
    result = CliRunner().invoke(app, ["collect", "--help"])

    assert result.exit_code == 0
    assert "--sso-profile" in result.output
    assert "--role-arn" not in result.output
    assert "--region" not in result.output
