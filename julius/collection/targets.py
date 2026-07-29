"""Escopo explícito e verificável de contas AWS para execução pelo Devin."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from julius.collection.session import make_session

_AWS_REGION = "sa-east-1"
DEFAULT_ACCOUNT_TARGETS_PATH = "~/.julius-accounts.json"


class AccountTargetError(ValueError):
    """Cadastro multi-conta inseguro, ambíguo ou incompatível com STS."""


@dataclass(frozen=True)
class AccountTarget:
    name: str
    expected_account_id: str
    sso_profile: str
    enabled: bool


@dataclass(frozen=True)
class VerifiedAccount:
    name: str
    account_id: str
    caller_arn: str
    sso_profile: str
    region: str
    credential_source: str


def load_account_targets(
    path: str | Path, *, require_enabled: bool = True
) -> list[AccountTarget]:
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(f"cadastro de contas não encontrado: {file_path}")
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
        raise AccountTargetError("schema_version do cadastro de contas deve ser 1.0")
    items = raw.get("accounts")
    if not isinstance(items, list):
        raise AccountTargetError("accounts deve ser uma lista")
    expected_fields = {"name", "expected_account_id", "sso_profile", "enabled"}
    targets = []
    names: set[str] = set()
    profiles: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise AccountTargetError(
                "conta possui campos ausentes ou não permitidos"
            )
        if not isinstance(item["enabled"], bool):
            raise AccountTargetError("enabled deve ser booleano")
        values = {key: item[key] for key in expected_fields - {"enabled"}}
        if not all(isinstance(value, str) for value in values.values()):
            raise AccountTargetError("campos da conta devem ser texto")
        name = item["name"].strip()
        account_id = item["expected_account_id"].strip()
        sso_profile = item["sso_profile"].strip()
        if not name or name in names:
            raise AccountTargetError(f"nome de conta vazio ou duplicado: {name}")
        if not (account_id.isdigit() and len(account_id) == 12):
            raise AccountTargetError(f"account_id AWS inválido: {account_id}")
        if sso_profile in profiles:
            raise AccountTargetError(
                f"perfil SSO vazio ou duplicado no cadastro: {name}"
            )
        names.add(name)
        profiles.add(sso_profile)
        targets.append(
            AccountTarget(
                name=name,
                expected_account_id=account_id,
                sso_profile=sso_profile,
                enabled=item["enabled"],
            )
        )
    enabled = [target for target in targets if target.enabled]
    if not enabled and require_enabled:
        raise AccountTargetError("nenhuma conta está habilitada no cadastro")
    return enabled


def resolve_account_name(
    *,
    explicit_name: str = "",
    sso_profile: str = "",
    config_path: str | Path = DEFAULT_ACCOUNT_TARGETS_PATH,
) -> str:
    """Resolve o nome lógico sem pedir acesso ao AWS Organizations.

    O cadastro local já liga `sso_profile`, Account ID e nome. Um valor
    explícito continua soberano; sem cadastro, o perfil preserva o fallback
    histórico do CLI.
    """
    if explicit_name.strip():
        return explicit_name.strip()
    profile = sso_profile.strip()
    path = Path(config_path).expanduser()
    if profile and path.exists():
        for target in load_account_targets(path, require_enabled=False):
            if target.sso_profile == profile:
                return target.name
    return profile


def verify_account_targets(
    targets: list[AccountTarget],
    *,
    session_factory=make_session,
) -> tuple[list[VerifiedAccount], list[str]]:
    """As contas cujo SSO responde, e o motivo de cada uma que não respondeu.

    Um perfil com sessão SSO expirada é o caso comum de quem roda multi-conta —
    e antes ele abortava a verificação de **todas**, inclusive das contas cujo
    login estava válido. Cada perfil é isolado, e quem chama decide se segue com
    as que deram certo.

    A divergência de identidade continua sendo erro e não um perfil a ignorar:
    coletar a conta errada com o nome certo é o problema que esta função existe
    para impedir.
    """
    verified: list[VerifiedAccount] = []
    falhas: list[str] = []
    for target in targets:
        try:
            session = session_factory(target.sso_profile or None, _AWS_REGION)
            identity = session.client("sts").get_caller_identity()
        except AccountTargetError:
            raise
        except Exception as exc:
            falhas.append(f"{target.name}: {type(exc).__name__}")
            continue
        account_id = str(identity.get("Account") or "")
        if account_id != target.expected_account_id:
            raise AccountTargetError(
                f"identidade SSO ativa para {target.name} resolveu para "
                f"{account_id or 'desconhecido'}, "
                f"esperado {target.expected_account_id}"
            )
        verified.append(
            VerifiedAccount(
                name=target.name,
                account_id=account_id,
                caller_arn=str(identity.get("Arn") or ""),
                sso_profile=target.sso_profile,
                region=_AWS_REGION,
                credential_source="aws_cli_sso_profile",
            )
        )
    return verified, falhas


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
