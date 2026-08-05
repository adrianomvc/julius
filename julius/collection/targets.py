"""Escopo explícito e verificável de contas AWS para execução pelo Devin."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from julius.collection.policy import CONSUMER_DATAMESH, policy_for_profile
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
    scope_profile: str = CONSUMER_DATAMESH
    athena_workgroups: tuple[str, ...] = ()
    athena_workgroup_roles: tuple[tuple[str, str], ...] = ()


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
    if not isinstance(raw, dict) or raw.get("schema_version") not in {
        "1.0",
        "1.1",
        "1.2",
        "1.3",
    }:
        raise AccountTargetError(
            "schema_version do cadastro de contas deve ser 1.0, 1.1, 1.2 ou 1.3"
        )
    items = raw.get("accounts")
    if not isinstance(items, list):
        raise AccountTargetError("accounts deve ser uma lista")
    required_fields = {"name", "expected_account_id", "sso_profile", "enabled"}
    allowed_fields = required_fields | {"scope_profile", "athena_workgroups"}
    targets = []
    names: set[str] = set()
    profiles: set[str] = set()
    for item in items:
        if (
            not isinstance(item, dict)
            or not required_fields.issubset(item)
            or not set(item).issubset(allowed_fields)
        ):
            raise AccountTargetError(
                "conta possui campos ausentes ou não permitidos"
            )
        if not isinstance(item["enabled"], bool):
            raise AccountTargetError("enabled deve ser booleano")
        values = {key: item[key] for key in required_fields - {"enabled"}}
        if not all(isinstance(value, str) for value in values.values()):
            raise AccountTargetError("campos da conta devem ser texto")
        name = item["name"].strip()
        account_id = item["expected_account_id"].strip()
        sso_profile = item["sso_profile"].strip()
        scope_profile = str(item.get("scope_profile") or CONSUMER_DATAMESH).strip()
        workgroups_raw = item.get("athena_workgroups", [])
        if not isinstance(workgroups_raw, list):
            raise AccountTargetError("athena_workgroups deve ser uma lista")
        parsed_workgroups: list[tuple[str, str]] = []
        allowed_roles = {"preferred", "legacy", "unused_expected", "unclassified"}
        for value in workgroups_raw:
            if isinstance(value, str):
                name, role = value.strip(), "unclassified"
            elif (
                isinstance(value, dict)
                and set(value) == {"name", "role"}
                and isinstance(value["name"], str)
                and isinstance(value["role"], str)
            ):
                name, role = value["name"].strip(), value["role"].strip()
            else:
                raise AccountTargetError(
                    "athena_workgroups aceita nomes ou objetos {name, role}"
                )
            if name:
                parsed_workgroups.append((name, role or "unclassified"))
        athena_workgroups = tuple(dict.fromkeys(name for name, _ in parsed_workgroups))
        athena_workgroup_roles = tuple(
            (name, next(role for candidate, role in parsed_workgroups if candidate == name))
            for name in athena_workgroups
        )
        if any(
            re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None
            for value in athena_workgroups
        ):
            raise AccountTargetError("nome inválido em athena_workgroups")
        if any(role not in allowed_roles for _, role in athena_workgroup_roles):
            raise AccountTargetError("role inválido em athena_workgroups")
        try:
            policy_for_profile(scope_profile)
        except ValueError as exc:
            raise AccountTargetError(str(exc)) from exc
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
                scope_profile=scope_profile,
                athena_workgroups=athena_workgroups,
                athena_workgroup_roles=athena_workgroup_roles,
            )
        )
    enabled = [target for target in targets if target.enabled]
    if not enabled and require_enabled:
        raise AccountTargetError("nenhuma conta está habilitada no cadastro")
    return enabled


def _resolve_account_name(
    explicit_name: str,
    sso_profile: str,
    config_path: str | Path,
) -> tuple[str, str]:
    """O nome lógico e **de onde ele veio**, numa cascata só.

    A procedência importa porque o último degrau não é um nome de conta: é o
    apelido que alguém deu ao perfil em `aws configure sso`. Quem chama precisa
    poder distinguir "o operador declarou isto" de "não sobrou mais nada", e é
    essa distinção que autoriza a coleta a buscar o nome na própria AWS.

    Uma função só devolvendo os dois, em vez de duas percorrendo a mesma
    cascata: duas cópias divergiriam no dia em que um degrau mudasse.
    """
    if explicit_name.strip():
        return explicit_name.strip(), "explicit"
    profile = sso_profile.strip()
    path = Path(config_path).expanduser()
    if profile and path.exists():
        for target in load_account_targets(path, require_enabled=False):
            if target.sso_profile == profile:
                return target.name, "registry"
    return profile, "profile"


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
    return _resolve_account_name(explicit_name, sso_profile, config_path)[0]


def resolve_account_name_source(
    *,
    explicit_name: str = "",
    sso_profile: str = "",
    config_path: str | Path = DEFAULT_ACCOUNT_TARGETS_PATH,
) -> str:
    """De onde o nome veio: `explicit`, `registry` ou `profile`.

    `profile` é o único que não é uma declaração sobre a conta — e é por isso
    que ele é o único que a coleta pode substituir pelo nome que a AWS informa.
    """
    return _resolve_account_name(explicit_name, sso_profile, config_path)[1]


def resolve_scope_profile(
    *,
    explicit_profile: str = "",
    sso_profile: str = "",
    config_path: str | Path = DEFAULT_ACCOUNT_TARGETS_PATH,
) -> str:
    """CLI explícito vence cadastro; conta cadastrada antiga assume Consumer."""
    if explicit_profile.strip():
        return policy_for_profile(explicit_profile).profile
    profile = sso_profile.strip()
    path = Path(config_path).expanduser()
    if profile and path.exists():
        for target in load_account_targets(path, require_enabled=False):
            if target.sso_profile == profile:
                return target.scope_profile
    return policy_for_profile(None).profile


def resolve_athena_workgroups(
    *,
    explicit_names: str = "",
    sso_profile: str = "",
    config_path: str | Path = DEFAULT_ACCOUNT_TARGETS_PATH,
) -> tuple[str, ...]:
    """CLI explícito vence cadastro; vazio mantém descoberta automática."""
    if explicit_names.strip():
        names = tuple(
            dict.fromkeys(
                value.strip() for value in explicit_names.split(",") if value.strip()
            )
        )
        if any(
            re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None for value in names
        ):
            raise AccountTargetError("nome inválido em --athena-history-workgroups")
        return names
    profile = sso_profile.strip()
    path = Path(config_path).expanduser()
    if profile and path.exists():
        for target in load_account_targets(path, require_enabled=False):
            if target.sso_profile == profile:
                return target.athena_workgroups
    return ()


def resolve_athena_workgroup_roles(
    *,
    sso_profile: str = "",
    config_path: str | Path = DEFAULT_ACCOUNT_TARGETS_PATH,
) -> dict[str, str]:
    """Contexto operacional; não altera uso, custo ou prioridade."""
    profile = sso_profile.strip()
    path = Path(config_path).expanduser()
    if profile and path.exists():
        for target in load_account_targets(path, require_enabled=False):
            if target.sso_profile == profile:
                return dict(target.athena_workgroup_roles)
    return {}


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
