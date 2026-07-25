"""Coleta read-only do Athena na janela de análise.

Este módulo só orquestra: cada etapa mora num módulo próprio — execuções,
catálogo, atores, custo e agregação. Execuções e SQL original existem somente
em memória; a saída contém padrões sanitizados, cobertura, reconciliação e uso
agregado por ator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from julius.collection.collectors.athena import actors as actors_step
from julius.collection.collectors.athena import aggregate, catalog, cost
from julius.collection.collectors.athena import executions as executions_step
from julius.collection.collectors.athena.evidence import _DDL, AthenaExecutionEvidence
from julius.collection.models import AthenaActorUsage, AthenaCoverage, AthenaQuery
from julius.collection.window import AnalysisWindow
from julius.config import ANALYSIS_WINDOW_DAYS


@dataclass
class AthenaAnalysis:
    queries: list[AthenaQuery]
    actors: list[AthenaActorUsage]
    coverage: AthenaCoverage


def complete_utc_window(
    now: datetime | None = None, days: int = ANALYSIS_WINDOW_DAYS
) -> tuple[datetime, datetime]:
    """Compatibilidade: a definição canônica agora é `AnalysisWindow`."""
    window = AnalysisWindow.trailing(days=days, now=now)
    return window.start, window.end


def collect_analysis(
    athena_client,
    *,
    cloudwatch_client=None,
    cloudtrail_client=None,
    identitystore_client=None,
    glue_client=None,
    s3_client=None,
    ce_client=None,
    window: AnalysisWindow | None = None,
    lookback_days: int = ANALYSIS_WINDOW_DAYS,
    max_ids_per_workgroup: int | None = None,
    now: datetime | None = None,
) -> AthenaAnalysis:
    window = window or AnalysisWindow.trailing(days=lookback_days, now=now)
    start, end = window.start, window.end
    coverage = AthenaCoverage(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        window_days=window.days,
    )

    evidence = _collect_executions(
        athena_client, coverage, window, max_ids_per_workgroup
    )
    if evidence:
        coverage.oldest_submission = min(
            item.submitted_at for item in evidence
        ).isoformat()

    catalog.enrich_catalog(evidence, glue_client, s3_client, coverage.gaps)
    actors_step.enrich_actors(
        evidence, cloudtrail_client, identitystore_client, start, end, coverage.gaps
    )

    coverage.api_scanned_bytes = sum(
        item.scanned_bytes
        for item in evidence
        if item.modality == "on_demand" and item.statement_type not in _DDL
    )
    coverage.api_billed_bytes = sum(
        item.billed_bytes for item in evidence if item.modality == "on_demand"
    )
    cost.reconcile_cloudwatch(
        coverage, cloudwatch_client, coverage.workgroups, start, end
    )
    _allocate_cost(evidence, coverage, ce_client, start, end)

    queries = aggregate.aggregate_queries(evidence, coverage)
    actors = aggregate.aggregate_actors(evidence, queries, coverage)
    return AthenaAnalysis(queries=queries, actors=actors, coverage=coverage)


def collect_queries(
    athena_client,
    *,
    lookback_days: int = ANALYSIS_WINDOW_DAYS,
    max_ids: int | None = None,
    now: datetime | None = None,
) -> list[AthenaQuery]:
    """Compatibilidade: inventários antigos esperam apenas a lista de queries."""
    return collect_analysis(
        athena_client,
        lookback_days=lookback_days,
        max_ids_per_workgroup=max_ids,
        now=now,
    ).queries


def _collect_executions(
    athena_client,
    coverage: AthenaCoverage,
    window: AnalysisWindow,
    max_ids_per_workgroup: int | None,
) -> list[AthenaExecutionEvidence]:
    workgroups, configs = executions_step.workgroups(athena_client, coverage)
    evidence: list[AthenaExecutionEvidence] = []
    for workgroup in workgroups:
        ids, truncated = executions_step.execution_ids(
            athena_client,
            workgroup,
            max_ids=max_ids_per_workgroup,
            gaps=coverage.gaps,
        )
        coverage.truncated = coverage.truncated or truncated
        if ids is None:
            continue
        coverage.workgroups_covered += 1
        for raw in executions_step.query_executions(
            athena_client, ids, coverage.gaps
        ):
            item = executions_step.execution(raw, configs.get(workgroup, {}))
            if item and window.start <= item.submitted_at < window.end:
                evidence.append(item)
    return evidence


def _allocate_cost(
    evidence: list[AthenaExecutionEvidence],
    coverage: AthenaCoverage,
    ce_client,
    start: datetime,
    end: datetime,
) -> None:
    daily_cost, metric, currency, isolated = cost.costs(
        ce_client, start, end, coverage.gaps
    )
    coverage.cost_metric, coverage.currency = metric, currency
    coverage.net_cost = round(sum(daily_cost.values()), 6) if daily_cost else None
    reconciled = isolated and cost.reconciled(coverage)
    allocation_complete = cost.allocate(evidence, daily_cost, reconciled)
    if reconciled and allocation_complete:
        coverage.cost_quality = "reconciled"
    elif daily_cost:
        coverage.cost_quality = "partial"
