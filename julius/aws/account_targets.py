"""Escopo explícito e verificável de contas AWS para execução pelo Devin."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from julius.aws.session import assume_role, make_session

_ROLE_ARN = re.compile(r"^arn:aws[a-zA-Z-]*:iam::\d{12}:role/.+$")


class AccountTargetError(ValueError):
    """Cadastro multi-conta inseguro, ambíguo ou incompatível com STS."""


@dataclass(frozen=True)
class AccountTarget:
    name: str
    expected_account_id: str
    profile: str
    region: str
    role_arn: str
    enabled: bool


@dataclass(frozen=True)
class VerifiedAccount:
    name: str
    account_id: str
    caller_arn: str
    profile: str
    region: str
    role_arn: str


def load_account_targets(path: str | Path) -> list[AccountTarget]:
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(f"cadastro de contas não encontrado: {file_path}")
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
        raise AccountTargetError("schema_version do cadastro de contas deve ser 1.0")
    items = raw.get("accounts")
    if not isinstance(items, list):
        raise AccountTargetError("accounts deve ser uma lista")
    expected_fields = {
        "name",
        "expected_account_id",
        "profile",
        "region",
        "role_arn",
        "enabled",
    }
    targets = []
    names: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise AccountTargetError(
                "conta possui campos ausentes ou não permitidos"
            )
        if not isinstance(item["enabled"], bool):
            raise AccountTargetError("enabled deve ser booleano")
        values = {
            key: item[key]
            for key in expected_fields - {"enabled"}
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise AccountTargetError("campos da conta devem ser texto")
        name = item["name"].strip()
        account_id = item["expected_account_id"].strip()
        profile = item["profile"].strip()
        region = item["region"].strip()
        role_arn = item["role_arn"].strip()
        if not name or name in names:
            raise AccountTargetError(f"nome de conta vazio ou duplicado: {name}")
        if not (account_id.isdigit() and len(account_id) == 12):
            raise AccountTargetError(f"account_id AWS inválido: {account_id}")
        if not region:
            raise AccountTargetError(f"região ausente para: {name}")
        if role_arn and not _ROLE_ARN.fullmatch(role_arn):
            raise AccountTargetError(f"role ARN inválida para: {name}")
        identity = (profile, role_arn)
        if identity in identities:
            raise AccountTargetError(
                f"perfil/role duplicado no cadastro: {name}"
            )
        names.add(name)
        identities.add(identity)
        targets.append(
            AccountTarget(
                name=name,
                expected_account_id=account_id,
                profile=profile,
                region=region,
                role_arn=role_arn,
                enabled=item["enabled"],
            )
        )
    enabled = [target for target in targets if target.enabled]
    if not enabled:
        raise AccountTargetError("nenhuma conta está habilitada no cadastro")
    return enabled


def verify_account_targets(
    targets: list[AccountTarget],
    *,
    session_factory=make_session,
    assume_role_factory=assume_role,
) -> list[VerifiedAccount]:
    verified = []
    for target in targets:
        session = session_factory(target.profile or None, target.region)
        if target.role_arn:
            session = assume_role_factory(
                session,
                target.role_arn,
                target.region,
            )
        identity = session.client("sts").get_caller_identity()
        account_id = str(identity.get("Account") or "")
        if account_id != target.expected_account_id:
            raise AccountTargetError(
                f"perfil/role de {target.name} resolveu para {account_id or 'desconhecido'}, "
                f"esperado {target.expected_account_id}"
            )
        verified.append(
            VerifiedAccount(
                name=target.name,
                account_id=account_id,
                caller_arn=str(identity.get("Arn") or ""),
                profile=target.profile,
                region=target.region,
                role_arn=target.role_arn,
            )
        )
    return verified


def write_verified_accounts(
    accounts: list[VerifiedAccount],
    output_path: str | Path,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "verified_with": "sts:GetCallerIdentity",
        "read_only": True,
        "accounts": [asdict(account) for account in accounts],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
