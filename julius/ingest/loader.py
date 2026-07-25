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
    AthenaActorUsage,
    AthenaCoverage,
    AthenaQuery,
    CollectionHealth,
    DataBrewJob,
    GlueCostCoverage,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    InteractiveSession,
    PreviousResult,
    ProcessCost,
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


def _usd_record(raw: dict) -> dict | None:
    """Aceita o registro só quando ele está declaradamente em USD.

    A AWS reporta custo em USD, então todo dataset gerado pelo `julius collect`
    já vem em USD. Um registro sem `currency` é anterior a esse contrato e um
    registro em outra moeda não pode ser reexpresso sem câmbio — em ambos os
    casos ele é recusado, para não virar número sem procedência no relatório.
    """
    declared = str(raw.get("currency") or "").upper()
    if declared != "USD":
        return None
    return dict(raw)


def _service_cost(raw: dict) -> ServiceCost | None:
    normalized = _usd_record(raw)
    if normalized is None:
        return None
    normalized.setdefault("period_kind", "monthly")
    return _pick(normalized, ServiceCost)


def _previous_result(raw: dict) -> PreviousResult | None:
    normalized = _usd_record(raw)
    if normalized is None:
        return None
    return _pick(normalized, PreviousResult)


def load_account(path: str | Path) -> Account:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    account = Account(
        account_id=raw["account"],
        region=raw.get("region", "sa-east-1"),
        period=raw.get("period", ""),
        lookback_days=raw.get("lookback_days", 90),
        generated_at=raw.get("generated_at", ""),
        # USD é a única moeda aceita; a AWS já reporta custo em USD.
        currency="USD",
    )
    account.collection_health = [
        _pick(item, CollectionHealth)
        for item in raw.get("collection_health", [])
    ]
    ce = raw.get("cost_explorer", {})
    account.services = [
        service
        for service in (_service_cost(s) for s in ce.get("services", []))
        if service is not None
    ]
    account.glue_jobs = [_pick(j, GlueJob) for j in raw.get("glue_jobs", [])]
    account.interactive_sessions = [
        _pick(s, InteractiveSession) for s in raw.get("interactive_sessions", [])
    ]
    account.glue_crawlers = [
        _pick(c, GlueCrawler) for c in raw.get("glue_crawlers", [])
    ]
    account.glue_triggers = [
        _pick(t, GlueTrigger) for t in raw.get("glue_triggers", [])
    ]
    account.databrew_jobs = [
        _pick(j, DataBrewJob) for j in raw.get("databrew_jobs", [])
    ]
    account.process_costs = [
        _pick(p, ProcessCost) for p in raw.get("process_costs", [])
    ]
    account.athena_queries = [_pick(q, AthenaQuery) for q in raw.get("athena_queries", [])]
    if raw.get("athena_coverage"):
        account.athena_coverage = _pick(raw["athena_coverage"], AthenaCoverage)
    if raw.get("glue_cost_coverage"):
        account.glue_cost_coverage = _pick(
            raw["glue_cost_coverage"], GlueCostCoverage
        )
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
    account.previous_results = [
        result
        for result in (_previous_result(r) for r in gov.get("previous_results", []))
        if result is not None
    ]
    return account
