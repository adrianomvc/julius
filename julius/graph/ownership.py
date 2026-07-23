"""Resolução determinística de ownership por precedência."""

from __future__ import annotations

from dataclasses import dataclass

from julius.inventory.model import Account


@dataclass(frozen=True)
class OwnerAttribution:
    owner: str | None
    source: str
    confidence: float


def resolve_owner(account: Account, asset_type: str, asset_name: str) -> OwnerAttribution:
    asset = _asset(account, asset_type, asset_name)
    owner_tag = getattr(asset, "owner_tag", None) if asset is not None else None
    if owner_tag:
        return OwnerAttribution(owner_tag, "tag oficial", 1.0)

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

    return OwnerAttribution(None, "desconhecido", 0.0)


def _asset(account: Account, asset_type: str, asset_name: str):
    collections = {
        "glue_job": account.glue_jobs,
        "glue_session": account.interactive_sessions,
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


def asset_owner_tag(account: Account, asset_type: str, asset_name: str) -> str | None:
    asset = _asset(account, asset_type, asset_name)
    return getattr(asset, "owner_tag", None) if asset is not None else None
