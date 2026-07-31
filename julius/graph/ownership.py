"""Resolução determinística de ownership por precedência."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from julius.collection.models import Account


@dataclass(frozen=True)
class OwnerAttribution:
    owner: str | None
    source: str
    confidence: float
    event_time: str = ""
    event_name: str = ""


def resolve_owner(account: Account, asset_type: str, asset_name: str) -> OwnerAttribution:
    asset = _asset(account, asset_type, asset_name)
    owner_tag = getattr(asset, "owner_tag", None) if asset is not None else None
    if owner_tag:
        return OwnerAttribution(owner_tag, "tag oficial", 1.0)

    mutation_events = [
        event
        for event in account.actor_events
        if event.resource_type == asset_type
        and event.resource_name == asset_name
        and _human_event(event)
        and event.event_name.startswith(("Create", "Update", "Put"))
    ]
    updates = sorted(
        (event for event in mutation_events if event.event_name.startswith(("Update", "Put"))),
        key=lambda event: event.event_time,
        reverse=True,
    )
    creates = sorted(
        (event for event in mutation_events if event.event_name.startswith("Create")),
        key=lambda event: event.event_time,
    )
    if updates:
        person = _person(updates[0])
        if person:
            return OwnerAttribution(
                person,
                f"última alteração humana ({updates[0].event_name})",
                0.9,
                updates[0].event_time,
                updates[0].event_name,
            )
    if creates:
        person = _person(creates[0])
        if person:
            return OwnerAttribution(
                person,
                f"criador identificado ({creates[0].event_name})",
                0.85,
                creates[0].event_time,
                creates[0].event_name,
            )

    if asset_type == "table" and asset is not None:
        if asset.corporate_owner:
            return OwnerAttribution(asset.corporate_owner, "tabela corporativa de ativos", 0.95)
        if asset.datawarm_owner:
            return OwnerAttribution(asset.datawarm_owner, "configuração DataWarm", 0.9)
        writer = account.job_by_name(asset.written_by)
        if writer and writer.owner_tag:
            return OwnerAttribution(writer.owner_tag, "Squad responsável pelo job escritor", 0.85)
        if asset.primary_community:
            return OwnerAttribution(asset.primary_community, "principal comunidade que toca a tabela", 0.6)

    convencao = owner_from_name(asset_name)
    if convencao:
        return OwnerAttribution(
            convencao, f"convenção de nome do recurso ({asset_name})", 0.4
        )

    return OwnerAttribution(None, "desconhecido", 0.0)


#: Convenção de nome que denuncia o time dono. É a última tentativa, abaixo de
#: qualquer tag, de CloudTrail e do cadastro corporativo — um nome é intenção de
#: quem criou o recurso, não declaração de propriedade, e pode estar velho.
#:
#: Vale mesmo assim porque `check_actionable` exige dono ou ator: sem nenhum dos
#: dois o achado nasce em `investigar_primeiro` por falta de responsável, e não
#: por falta de evidência. Resolver o dono move o achado de fila sem alterar um
#: centavo de economia.
OWNER_NAME_PATTERNS: tuple[str, ...] = (
    r"^(squad[-_][a-z0-9]+)",
    r"^(team[-_][a-z0-9]+)",
    r"^(time[-_][a-z0-9]+)",
    r"^(tribo?[-_][a-z0-9]+)",
    r"^(grupo[-_][a-z0-9]+)",
)

_NAME_PATTERNS = tuple(re.compile(padrao, re.IGNORECASE) for padrao in OWNER_NAME_PATTERNS)


def owner_from_name(asset_name: str) -> str | None:
    """Time deduzido do nome do recurso, quando ele segue convenção declarada."""
    nome = str(asset_name or "").strip()
    for padrao in _NAME_PATTERNS:
        encontrado = padrao.match(nome)
        if encontrado:
            return encontrado.group(1).lower()
    return None


def _asset(account: Account, asset_type: str, asset_name: str) -> Any:
    collections: dict[str, list[Any]] = {
        "glue_job": account.glue_jobs,
        "glue_session": account.interactive_sessions,
        "glue_crawler": account.glue_crawlers,
        "glue_trigger": account.glue_triggers,
        "databrew_job": account.databrew_jobs,
        "athena_query": account.athena_queries,
        "state_machine": account.state_machines,
        "sagemaker_app": account.sagemaker_apps,
        "sagemaker_endpoint": account.sagemaker_endpoints,
        "table": account.tables,
        "schedule": account.schedules,
    }
    id_fields = {
        "glue_session": "session_id",
        "athena_query": "query_id",
    }
    field_name = id_fields.get(asset_type, "name")
    return next(
        (
            item
            for item in collections.get(asset_type, [])
            if getattr(item, field_name, None) == asset_name
        ),
        None,
    )


def _person(event) -> str | None:
    if event.source_identity:
        return event.source_identity
    arn = event.user_arn or ""
    if ":assumed-role/" in arn:
        candidate = arn.rsplit("/", 1)[-1].strip()
        return candidate or None
    if ":user/" in arn:
        return arn.rsplit("/", 1)[-1].strip() or None
    return None


def _human_event(event) -> bool:
    if event.is_human:
        return True
    # Compatibilidade com datasets anteriores ao campo `is_human`.
    candidate = str(event.source_identity or event.user_arn or "").lower()
    if not candidate:
        return False
    automated = (
        "cloudformation",
        "terraform",
        "pipeline",
        "codebuild",
        "github",
        "service-role",
        "aws-service-role",
        "botocore",
    )
    return not any(token in candidate for token in automated)


def asset_owner_tag(account: Account, asset_type: str, asset_name: str) -> str | None:
    asset = _asset(account, asset_type, asset_name)
    return getattr(asset, "owner_tag", None) if asset is not None else None
