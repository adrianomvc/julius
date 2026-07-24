"""Escopo explícito e verificação STS para uma ou várias contas."""

from __future__ import annotations

import json

import pytest

from julius.aws.account_targets import (
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


def _target(name, account_id, profile, *, enabled=True):
    return {
        "name": name,
        "expected_account_id": account_id,
        "profile": profile,
        "region": "sa-east-1",
        "role_arn": "",
        "enabled": enabled,
    }


def test_loads_only_explicitly_enabled_accounts(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("principal", "123456789012", ""),
            _target("desabilitada", "999999999999", "disabled", enabled=False),
        ],
    )

    targets = load_account_targets(path)
    assert [target.name for target in targets] == ["principal"]


def test_verifies_each_profile_against_expected_sts_account(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("a", "123456789012", "profile-a"),
            _target("b", "210987654321", "profile-b"),
        ],
    )
    account_by_profile = {
        "profile-a": "123456789012",
        "profile-b": "210987654321",
    }

    verified = verify_account_targets(
        load_account_targets(path),
        session_factory=lambda profile, _region: FakeSession(account_by_profile[profile]),
    )
    manifest = write_verified_accounts(verified, tmp_path / "verified.json")
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert [account.account_id for account in verified] == [
        "123456789012",
        "210987654321",
    ]
    assert payload["read_only"] is True
    assert payload["verified_with"] == "sts:GetCallerIdentity"


def test_stops_on_profile_account_mismatch(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(path, [_target("a", "123456789012", "profile-a")])

    with pytest.raises(AccountTargetError, match="esperado"):
        verify_account_targets(
            load_account_targets(path),
            session_factory=lambda *_: FakeSession("999999999999"),
        )


def test_rejects_implicit_or_ambiguous_scope(tmp_path):
    path = tmp_path / "accounts.json"
    _write_config(
        path,
        [
            _target("a", "123456789012", "", enabled=False),
            _target("b", "210987654321", "profile-b", enabled=False),
        ],
    )
    with pytest.raises(AccountTargetError, match="nenhuma conta"):
        load_account_targets(path)
