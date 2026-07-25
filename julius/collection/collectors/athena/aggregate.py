"""Agregação sanitizada: execuções viram padrões de query e uso por ator."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from julius.collection.collectors.athena.evidence import (
    AthenaExecutionEvidence,
    percentile,
    recurrence,
)
from julius.collection.models import AthenaActorUsage, AthenaQuery

_GB = 1024**3
_RESULT_REUSE_WINDOW = timedelta(minutes=60)


def aggregate_queries(items, coverage):
    grouped: dict[str, list[AthenaExecutionEvidence]] = defaultdict(list)
    for item in items:
        grouped[f"{item.workgroup}:{item.structural_fingerprint}"].append(item)
    output: list[AthenaQuery] = []
    for group in grouped.values():
        first = group[0]
        recurring, _, regular = recurrence(item.submitted_at for item in group)
        by_exact: dict[str, list[datetime]] = defaultdict(list)
        for item in group:
            by_exact[item.exact_fingerprint].append(item.submitted_at)
        burst = any(recurrence(stamps)[1] for stamps in by_exact.values())
        durations = sorted(item.duration_ms for item in group)
        actors = sorted({item.actor for item in group})
        billed = sum(item.billed_bytes for item in group)
        costs = [item.allocated_cost for item in group if item.allocated_cost is not None]
        avoidable = reuse_avoidable(group)
        partition_keys = sorted({key for item in group for key in item.partition_keys})
        missing = sorted({key for item in group for key in item.missing_partition_filters})
        formats = sorted({fmt for item in group for fmt in item.storage_formats})
        wide_tables = sorted({name for item in group for name in item.wide_tables})
        unpartitioned = sorted(
            {name for item in group for name in item.unpartitioned_tables}
        )
        row_uncompressed = sorted(
            {name for item in group for name in item.row_format_uncompressed}
        )
        columnar_uncompressed = sorted(
            {name for item in group for name in item.columnar_uncompressed}
        )
        codecs = sorted(
            {codec for item in group for codec in item.compression_codecs}
        )
        projection_candidates = sorted(
            {
                name
                for item in group
                for name in item.partition_projection_candidates
            }
        )
        planning = sorted(item.planning_ms for item in group)
        average_scanned = round(sum(i.scanned_bytes for i in group) / len(group))
        full_scan = (
            average_scanned >= _GB
            and all(
                not item.has_where or bool(item.missing_partition_filters)
                for item in group
            )
        )
        output.append(
            AthenaQuery(
                query_id=first.structural_fingerprint,
                workgroup=first.workgroup,
                statement=first.sanitized_sql,
                data_scanned_bytes=average_scanned,
                executions_per_month=len(group),
                has_partition_filter=not missing,
                table_is_partitioned=bool(partition_keys),
                selects_star=any(i.selects_star for i in group),
                result_reuse_enabled=any(i.reused for i in group),
                observed_runs=len(group),
                coverage_days=30,
                reads_tables=sorted({table for item in group for table in item.reads_tables}),
                exact_fingerprint=first.exact_fingerprint if len({i.exact_fingerprint for i in group}) == 1 else "",
                structural_fingerprint=first.structural_fingerprint,
                modality=first.modality if len({i.modality for i in group}) == 1 else "mixed",
                allocated_cost=round(sum(costs), 6) if costs else None,
                cost_quality=coverage.cost_quality,
                currency=coverage.currency,
                active_days=len({i.submitted_at.date() for i in group}),
                actor_count=len(actors),
                actors=actors,
                recurring=recurring,
                burst=burst,
                regular=regular,
                automated=all(i.actor_type == "automation" for i in group),
                p50_ms=round(statistics.median(durations)) if durations else 0,
                p95_ms=percentile(durations, 0.95),
                failed_runs=sum(i.state == "FAILED" for i in group),
                cancelled_runs=sum(i.state == "CANCELLED" for i in group),
                reused_runs=sum(i.reused for i in group),
                reuse_eligible_runs=avoidable["runs"],
                reuse_avoidable_billed_bytes=avoidable["bytes"],
                reuse_avoidable_cost=avoidable["cost"],
                billed_bytes=billed,
                avg_billed_bytes=round(billed / len(group)),
                partition_keys=partition_keys,
                missing_partition_filters=missing,
                storage_formats=formats,
                small_files_confirmed=any(i.small_files_confirmed for i in group),
                small_file_count=max((i.small_file_count for i in group), default=0),
                average_file_bytes=max((i.average_file_bytes for i in group), default=0),
                total_table_bytes=max((i.total_table_bytes for i in group), default=0),
                max_table_columns=max((i.max_table_columns for i in group), default=0),
                wide_tables=wide_tables,
                unpartitioned_tables=unpartitioned,
                full_scan_confirmed=full_scan,
                row_format_uncompressed=row_uncompressed,
                columnar_uncompressed=columnar_uncompressed,
                compression_codecs=codecs,
                partition_projection_enabled=any(
                    i.partition_projection_enabled for i in group
                ),
                partition_projection_candidates=projection_candidates,
                partition_count=max((i.partition_count for i in group), default=0),
                p95_planning_ms=percentile(planning, 0.95),
                parse_succeeded=all(i.parse_succeeded for i in group),
                evidence=query_evidence(group, missing),
            )
        )
    return sorted(output, key=lambda q: (q.allocated_cost or 0, q.billed_bytes), reverse=True)


def aggregate_actors(items, queries, coverage):
    refs: dict[str, list[str]] = defaultdict(list)
    for query in queries:
        for actor in query.actors:
            refs[actor].append(query.structural_fingerprint)
    grouped: dict[str, list[AthenaExecutionEvidence]] = defaultdict(list)
    for item in items:
        grouped[item.actor].append(item)
    output = []
    for actor, group in grouped.items():
        patterns = {item.structural_fingerprint for item in group}
        recurring = sum(
            recurrence(i.submitted_at for i in items if i.structural_fingerprint == pattern)[0]
            for pattern in patterns
        )
        bursts = sum(
            recurrence(i.submitted_at for i in items if i.structural_fingerprint == pattern)[1]
            for pattern in patterns
        )
        costs = [item.allocated_cost for item in group if item.allocated_cost is not None]
        output.append(
            AthenaActorUsage(
                actor=actor,
                actor_type=group[0].actor_type,
                identity_source=group[0].identity_source,
                identity_confidence=group[0].identity_confidence,
                email=group[0].actor_email,
                query_count=len(group),
                allocated_cost=round(sum(costs), 6) if costs else None,
                currency=coverage.currency,
                billed_bytes=sum(item.billed_bytes for item in group),
                active_days=len({item.submitted_at.date() for item in group}),
                recurring_patterns=recurring,
                bursts=bursts,
                selects_star=sum(item.selects_star for item in group),
                missing_partition_filters=sum(bool(item.missing_partition_filters) for item in group),
                full_scans=sum(
                    item.scanned_bytes >= _GB
                    and (not item.has_where or bool(item.missing_partition_filters))
                    for item in group
                ),
                unpartitioned_tables=sum(bool(item.unpartitioned_tables) for item in group),
                compression_findings=sum(
                    bool(item.row_format_uncompressed or item.columnar_uncompressed)
                    for item in group
                ),
                partition_projection_candidates=sum(
                    bool(item.partition_projection_candidates) for item in group
                ),
                failures=sum(item.state == "FAILED" for item in group),
                automated=group[0].actor_type == "automation",
                opportunity_refs=sorted(set(refs[actor])),
            )
        )
    return sorted(output, key=lambda actor: (actor.allocated_cost or 0, actor.billed_bytes), reverse=True)


def query_evidence(group, missing):
    evidence = [
        f"{len(group)} execuções em {len({item.submitted_at.date() for item in group})} dias",
        f"{len({item.actor for item in group})} atores no padrão",
        f"{sum(item.billed_bytes for item in group)} bytes faturáveis",
    ]
    if recurrence(item.submitted_at for item in group)[0]:
        evidence.append("padrão recorrente confirmado")
    if missing:
        evidence.append("sem filtro comprovado: " + ", ".join(missing))
    if any(item.reused for item in group):
        evidence.append(f"{sum(item.reused for item in group)} reutilizações confirmadas")
    avoidable = reuse_avoidable(group)
    if avoidable["runs"]:
        evidence.append(
            f"{avoidable['runs']} repetições exatas elegíveis em janela de 60 minutos"
        )
        evidence.append(
            f"{avoidable['bytes']} bytes faturáveis potencialmente evitáveis por result reuse"
        )
    if not all(item.parse_succeeded for item in group):
        evidence.append("parsing AST incompleto; recomendações semânticas bloqueadas")
    return evidence


def reuse_avoidable(group: list[AthenaExecutionEvidence]) -> dict[str, Any]:
    """Soma somente duplicatas exatas, elegíveis e próximas; nunca o padrão todo."""
    exact: dict[str, list[AthenaExecutionEvidence]] = defaultdict(list)
    for item in group:
        exact[item.exact_fingerprint].append(item)
    candidates: list[AthenaExecutionEvidence] = []
    for executions in exact.values():
        anchor: AthenaExecutionEvidence | None = None
        for item in sorted(executions, key=lambda value: value.submitted_at):
            if not item.reuse_eligible or item.state != "SUCCEEDED":
                continue
            if (
                anchor is not None
                and item.submitted_at - anchor.submitted_at <= _RESULT_REUSE_WINDOW
                and not item.reused
                and item.billed_bytes > 0
            ):
                candidates.append(item)
            anchor = item
    costs = [item.allocated_cost for item in candidates]
    cost = (
        round(sum(value for value in costs if value is not None), 6)
        if candidates and all(value is not None for value in costs)
        else None
    )
    return {
        "runs": len(candidates),
        "bytes": sum(item.billed_bytes for item in candidates),
        "cost": cost,
    }
