"""Carrega um dataset exportado (JSON) para o inventário normalizado.

No MVP 1A a fonte é um arquivo exportado; os coletores boto3 ao vivo preenchem
os mesmos dataclasses nas fases seguintes.
"""

from __future__ import annotations

import json
from pathlib import Path

from julius.inventory.model import (
    Account,
    ActorEvent,
    AthenaQuery,
    AthenaActorUsage,
    AthenaCoverage,
    GlueJob,
    InteractiveSession,
    PreviousResult,
    ProducerCandidate,
    SageMakerApp,
    SageMakerEndpoint,
    Schedule,
    ServiceCost,
    StateMachine,
    Table,
)


def _pick(d: dict, cls):
    """Instancia `cls` apenas com as chaves que ela conhece (tolerante a extras)."""
    fields = set(getattr(cls, "__dataclass_fields__", {}))
    return cls(**{k: v for k, v in d.items() if k in fields})


def load_account(path: str | Path) -> Account:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    account = Account(
        account_id=raw["account"],
        region=raw.get("region", "sa-east-1"),
        period=raw.get("period", ""),
        lookback_days=raw.get("lookback_days", 90),
        generated_at=raw.get("generated_at", ""),
        currency=raw.get("currency", "BRL"),
    )
    ce = raw.get("cost_explorer", {})
    account.services = [_pick(s, ServiceCost) for s in ce.get("services", [])]
    account.glue_jobs = [_pick(j, GlueJob) for j in raw.get("glue_jobs", [])]
    account.interactive_sessions = [
        _pick(s, InteractiveSession) for s in raw.get("interactive_sessions", [])
    ]
    account.athena_queries = [_pick(q, AthenaQuery) for q in raw.get("athena_queries", [])]
    if raw.get("athena_coverage"):
        account.athena_coverage = _pick(raw["athena_coverage"], AthenaCoverage)
    account.athena_actor_usage = [
        _pick(a, AthenaActorUsage) for a in raw.get("athena_actor_usage", [])
    ]
    account.state_machines = [_pick(s, StateMachine) for s in raw.get("state_machines", [])]
    account.sagemaker_apps = [_pick(a, SageMakerApp) for a in raw.get("sagemaker_apps", [])]
    account.sagemaker_endpoints = [
        _pick(e, SageMakerEndpoint) for e in raw.get("sagemaker_endpoints", [])
    ]
    account.tables = [_pick(t, Table) for t in raw.get("tables", [])]
    account.schedules = [_pick(s, Schedule) for s in raw.get("schedules", [])]
    account.actor_events = [_pick(e, ActorEvent) for e in raw.get("actor_events", [])]
    gov = raw.get("governance", {})
    account.producer_candidates = [
        _pick(p, ProducerCandidate) for p in gov.get("producer_candidates", [])
    ]
    account.previous_results = [_pick(r, PreviousResult) for r in gov.get("previous_results", [])]
    return account
