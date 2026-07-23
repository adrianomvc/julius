"""Orquestrador: coleta ao vivo (boto3) → Account (mesmo modelo do dataset exportado)."""

from __future__ import annotations

from datetime import date

import boto3

from julius.aws import (
    athena_collector,
    cloudtrail_collector,
    cloudwatch_collector,
    cost_explorer,
    datawarm_collector,
    glue_collector,
    schedules_collector,
    sessions_collector,
    stepfunctions_collector,
    touches_collector,
)
from julius.inventory.model import Account


def collect_account(
    session: boto3.Session,
    *,
    account_id: str | None = None,
    lookback_days: int = 90,
    touches_table: str = "",
    athena_workgroup: str = "julius",
    athena_output: str | None = None,
    include_cloudtrail: bool = False,
    datawarm_job: str = "",
) -> Account:
    region = session.region_name or "us-east-1"
    ident = account_id or _account_id(session)
    period = date.today().strftime("%b/%Y")

    glue = session.client("glue")
    account = Account(
        account_id=ident,
        region=region,
        period=period,
        lookback_days=lookback_days,
        generated_at=date.today().isoformat(),
    )
    account.services = _safe(lambda: cost_explorer.collect_services(session.client("ce")), [])
    account.glue_jobs = _safe(lambda: glue_collector.collect_jobs(glue, lookback_days=lookback_days), [])
    account.tables = _safe(lambda: glue_collector.collect_tables(glue), [])
    # Enriquece com CPU (CloudWatch) → destrava as regras de capacidade.
    _safe(
        lambda: cloudwatch_collector.enrich_glue_cpu(
            session.client("cloudwatch"), account.glue_jobs, lookback_days=lookback_days
        ),
        None,
    )
    account.interactive_sessions = _safe(lambda: sessions_collector.collect_sessions(glue), [])
    account.athena_queries = _safe(
        lambda: athena_collector.collect_queries(session.client("athena"), lookback_days=lookback_days), []
    )
    account.state_machines = _safe(
        lambda: stepfunctions_collector.collect_state_machines(
            session.client("stepfunctions"), lookback_days=lookback_days
        ),
        [],
    )
    account.schedules = _safe(
        lambda: schedules_collector.collect_schedules(session.client("events")),
        [],
    )
    # Toques (opcional): enriquece as tabelas com nº de acessos/consumidores.
    # Degradação graciosa: sem tabela de toques configurada, nada acontece.
    if touches_table:
        stats = _safe(
            lambda: touches_collector.collect_touches(
                session.client("athena"),
                touches_table=touches_table,
                workgroup=athena_workgroup,
                output_location=athena_output,
                lookback_days=lookback_days,
            ),
            {},
        )
        _merge_touches(account, stats)
    if datawarm_job:
        datawarm_collector.mark_publications(account, datawarm_job)
    if include_cloudtrail:
        account.actor_events = _safe(
            lambda: cloudtrail_collector.collect_actor_events(
                session.client("cloudtrail"), lookback_days=lookback_days
            ),
            [],
        )
    return account


def _merge_touches(account: Account, stats: dict) -> None:
    for table in account.tables:
        s = stats.get(table.name)
        if s is not None:
            table.touches_90d = s.touches
            table.consuming_accounts = s.accounts
            table.consuming_communities = s.communities
            table.used_by_accounts = list(s.account_ids)
            table.primary_community = s.primary_community


def _account_id(session: boto3.Session) -> str:
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception:
        return "unknown"


def _safe(fn, default):
    """Coleta best-effort: falha num serviço não derruba a coleta inteira."""
    try:
        return fn()
    except Exception:
        return default
