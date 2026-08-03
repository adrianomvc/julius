"""Coleta read-only do Athena na janela de análise.

Este módulo só orquestra: cada etapa mora num módulo próprio — execuções,
catálogo, atores, custo e agregação. Execuções e SQL original existem somente
em memória; a saída contém padrões sanitizados, cobertura, reconciliação e uso
agregado por ator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from julius.collection.collectors.athena import actors as actors_step
from julius.collection.collectors.athena import aggregate, catalog, cost
from julius.collection.collectors.athena import executions as executions_step
from julius.collection.collectors.athena.evidence import _DDL, AthenaExecutionEvidence
from julius.collection.collectors.athena.telemetry import AthenaTelemetry
from julius.collection.models import (
    AthenaActorUsage,
    AthenaCoverage,
    AthenaQuery,
    CollectionHealth,
)
from julius.collection.settings import ANALYSIS_WINDOW_DAYS
from julius.collection.window import AnalysisWindow


@dataclass
class AthenaAnalysis:
    queries: list[AthenaQuery]
    actors: list[AthenaActorUsage]
    coverage: AthenaCoverage
    # Uma entrada por dependência consultada — Athena API, CloudWatch,
    # CloudTrail, Identity Center, Glue Catalog, S3 e Cost Explorer.
    health: list[CollectionHealth] = field(default_factory=list)


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
    configured_workgroups: tuple[str, ...] = (),
    configured_workgroup_roles: dict[str, str] | None = None,
    now: datetime | None = None,
) -> AthenaAnalysis:
    window = window or AnalysisWindow.trailing(days=lookback_days, now=now)
    start, end = window.start, window.end
    coverage = AthenaCoverage(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        window_days=window.days,
    )
    telemetry = AthenaTelemetry(coverage)

    evidence = _collect_executions(
        athena_client,
        coverage,
        window,
        max_ids_per_workgroup,
        telemetry,
        s3_client=s3_client,
        configured_workgroups=configured_workgroups,
        configured_workgroup_roles=configured_workgroup_roles,
    )
    if evidence:
        coverage.oldest_submission = min(
            item.submitted_at for item in evidence
        ).isoformat()

    catalog.enrich_catalog(evidence, glue_client, s3_client, telemetry)
    actors_step.enrich_actors(
        evidence, cloudtrail_client, identitystore_client, start, end, telemetry
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
        coverage, cloudwatch_client, coverage.workgroups, start, end, telemetry
    )
    _allocate_cost(evidence, coverage, ce_client, start, end, telemetry)

    queries = aggregate.aggregate_queries(evidence, coverage)
    actors = aggregate.aggregate_actors(evidence, queries, coverage)
    return AthenaAnalysis(
        queries=queries,
        actors=actors,
        coverage=coverage,
        health=telemetry.entries(),
    )


def _collect_executions(
    athena_client,
    coverage: AthenaCoverage,
    window: AnalysisWindow,
    max_ids_per_workgroup: int | None,
    telemetry: AthenaTelemetry,
    s3_client=None,
    configured_workgroups: tuple[str, ...] = (),
    configured_workgroup_roles: dict[str, str] | None = None,
) -> list[AthenaExecutionEvidence]:
    telemetry.used("Athena API")
    workgroups, configs = executions_step.workgroups(
        athena_client,
        coverage,
        telemetry,
        configured=configured_workgroups,
        configured_roles=configured_workgroup_roles,
    )
    evidence: list[AthenaExecutionEvidence] = []
    for workgroup in workgroups:
        origem = "listing"
        ids, truncated = executions_step.execution_ids(
            athena_client,
            workgroup,
            max_ids=max_ids_per_workgroup,
            telemetry=telemetry,
        )
        if ids is None:
            # `ListQueryExecutions` é permissão à parte do `BatchGet`. Negá-la
            # zerava o workgroup inteiro, embora os IDs estejam nos nomes dos
            # resultados gravados e o `ProcessedBytes` venha da própria API.
            origem = "output_location"
            ids, truncated = executions_step.execution_ids_from_results(
                s3_client,
                coverage.workgroup_output_locations.get(workgroup, ""),
                modified_after=window.start,
                max_ids=max_ids_per_workgroup,
                telemetry=telemetry,
            )
        coverage.truncated = coverage.truncated or truncated
        if ids is None:
            continue
        coverage.execution_source[workgroup] = origem
        coverage.workgroups_covered += 1
        for raw in executions_step.query_executions(
            athena_client, ids, telemetry
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
    telemetry: AthenaTelemetry,
) -> None:
    daily_cost, metric, currency, isolated = cost.costs(
        ce_client, start, end, telemetry
    )
    coverage.cost_metric, coverage.currency = metric, currency
    coverage.net_cost = round(sum(daily_cost.values()), 6) if daily_cost else None
    reconciled = isolated and cost.reconciled(coverage, telemetry)
    allocation_complete = cost.allocate(evidence, daily_cost, reconciled)
    if reconciled and allocation_complete:
        coverage.cost_quality = "reconciled"
    elif daily_cost:
        coverage.cost_quality = "partial"
