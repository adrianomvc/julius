"""Coletor do histórico do Athena → AthenaQuery agregado.

Agrega execuções por assinatura da query (statement normalizado + workgroup):
bytes médios escaneados, nº de execuções, uso de partição/SELECT *. Result reuse
por query não vem no histórico; assume-se desligado (ajuste via get_work_group).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from julius.inventory.model import AthenaQuery

_LITERAL = re.compile(r"'[^']*'|\b\d+\b")
_WS = re.compile(r"\s+")


def _signature(statement: str) -> str:
    norm = _WS.sub(" ", _LITERAL.sub("?", statement)).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def collect_queries(
    athena_client, *, lookback_days: int = 90, max_ids: int = 1000, now: datetime | None = None
) -> list[AthenaQuery]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    months = max(1.0, lookback_days / 30.0)

    ids: list[str] = []
    paginator = athena_client.get_paginator("list_query_executions")
    for page in paginator.paginate():
        ids.extend(page.get("QueryExecutionIds", []))
        if len(ids) >= max_ids:
            ids = ids[:max_ids]
            break

    agg: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        resp = athena_client.batch_get_query_execution(QueryExecutionIds=ids[i : i + 50])
        for qe in resp.get("QueryExecutions", []):
            _accumulate(agg, qe, cutoff)

    out: list[AthenaQuery] = []
    for sig, a in agg.items():
        count = a["count"]
        stmt = a["statement"]
        out.append(
            AthenaQuery(
                query_id=a["name"] or sig,
                workgroup=a["workgroup"],
                statement=stmt[:200],
                data_scanned_bytes=round(a["scanned"] / count) if count else 0,
                executions_per_month=round(count / months, 1),
                has_partition_filter=bool(re.search(r"\bwhere\b", stmt, re.I)),
                table_is_partitioned=True,
                selects_star="select *" in stmt.lower(),
                result_reuse_enabled=False,
                observed_runs=count,
                coverage_days=int(months * 30),
            )
        )
    return out


def _accumulate(agg: dict, qe: dict, cutoff: datetime) -> None:
    status = qe.get("Status", {})
    submitted = status.get("SubmissionDateTime")
    if submitted and submitted.replace(tzinfo=submitted.tzinfo or timezone.utc) < cutoff:
        return
    if status.get("State") != "SUCCEEDED":
        return  # Athena não cobra queries que falham
    stmt = qe.get("Query", "")
    sig = _signature(stmt)
    stats = qe.get("Statistics", {})
    entry = agg.setdefault(
        sig,
        {"count": 0, "scanned": 0, "statement": stmt, "workgroup": qe.get("WorkGroup", "primary"), "name": None},
    )
    entry["count"] += 1
    entry["scanned"] += int(stats.get("DataScannedInBytes", 0) or 0)
