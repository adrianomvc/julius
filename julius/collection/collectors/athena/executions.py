"""Listagem e leitura das execuções de query, por workgroup.

É a única parte que chama a API do Athena; o resto do pacote trabalha
sobre a evidência que sai daqui."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from julius.collection.collectors.athena.evidence import (
    AthenaExecutionEvidence,
    billable_bytes,
    fingerprints,
)
from julius.collection.models import AthenaCoverage

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - instalação incompleta tem fallback seguro
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

_GB = 1024**3
_NONDETERMINISTIC_FUNCTIONS = {
    "current_date",
    "current_time",
    "current_timestamp",
    "localtime",
    "localtimestamp",
    "now",
    "rand",
    "random",
    "shuffle",
    "uuid",
}


def workgroups(client, coverage: AthenaCoverage) -> tuple[list[str], dict[str, dict]]:
    names: list[str] = []
    configs: dict[str, dict] = {}
    try:
        paginator = client.get_paginator("list_work_groups")
        for page in paginator.paginate():
            names.extend(item["Name"] for item in page.get("WorkGroups", []) if item.get("Name"))
    except Exception as exc:
        coverage.gaps.append(f"list_work_groups: {type(exc).__name__}")
        names = ["primary"]  # compatibilidade com mocks e permissões legadas
    names = list(dict.fromkeys(names))
    coverage.workgroups = names
    coverage.workgroups_total = len(names)
    for name in names:
        try:
            configs[name] = client.get_work_group(WorkGroup=name).get("WorkGroup", {})
            cfg = configs[name].get("Configuration", {})
            if not cfg.get("PublishCloudWatchMetricsEnabled", False):
                coverage.gaps.append(f"{name}: métricas CloudWatch desabilitadas")
        except Exception as exc:
            coverage.gaps.append(f"{name}: get_work_group {type(exc).__name__}")
    return names, configs


def execution_ids(client, workgroup: str, *, max_ids: int | None, gaps: list[str]):
    ids: list[str] = []
    truncated = False
    try:
        paginator = client.get_paginator("list_query_executions")
        try:
            pages = paginator.paginate(WorkGroup=workgroup)
        except TypeError:
            pages = paginator.paginate()  # mocks/inventários anteriores a workgroups
        for page in pages:
            ids.extend(page.get("QueryExecutionIds", []))
            if max_ids is not None and len(ids) >= max_ids:
                truncated = True
                ids = ids[:max_ids]
                break
        return ids, truncated
    except Exception as exc:
        gaps.append(f"{workgroup}: list_query_executions {type(exc).__name__}")
        return None, False


def query_executions(client, ids: list[str], gaps: list[str]):
    for index in range(0, len(ids), 50):
        chunk = ids[index : index + 50]
        try:
            response = client.batch_get_query_execution(QueryExecutionIds=chunk)
            yield from response.get("QueryExecutions", [])
            if response.get("UnprocessedQueryExecutionIds"):
                gaps.append(f"{len(response['UnprocessedQueryExecutionIds'])} execuções não processadas")
        except Exception:
            # Alguns ambientes autorizam Get mas não BatchGet.
            for query_id in chunk:
                try:
                    yield client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
                except Exception as exc:
                    gaps.append(f"get_query_execution: {type(exc).__name__}")


def execution(qe: dict, workgroup: dict) -> AthenaExecutionEvidence | None:
    status = qe.get("Status") or {}
    submitted = status.get("SubmissionDateTime")
    if not isinstance(submitted, datetime):
        return None
    submitted = submitted.replace(tzinfo=submitted.tzinfo or timezone.utc).astimezone(timezone.utc)
    stats = qe.get("Statistics") or {}
    reuse = bool((stats.get("ResultReuseInformation") or {}).get("ReusedPreviousResult"))
    state = str(status.get("State") or "UNKNOWN").upper()
    statement_type = str(qe.get("StatementType") or "").upper()
    scanned = int(stats.get("DataScannedInBytes") or 0)
    exact, structural, sanitized, parsed = fingerprints(str(qe.get("Query") or ""))
    modality = resolve_modality(qe, workgroup)
    sql = str(qe.get("Query") or "")
    return AthenaExecutionEvidence(
        query_execution_id=str(qe.get("QueryExecutionId") or ""),
        workgroup=str(qe.get("WorkGroup") or workgroup.get("Name") or "primary"),
        submitted_at=submitted,
        state=state,
        statement_type=statement_type,
        raw_sql=sql,
        exact_fingerprint=exact,
        structural_fingerprint=structural,
        sanitized_sql=sanitized,
        parse_succeeded=parsed,
        scanned_bytes=scanned,
        billed_bytes=billable_bytes(scanned, state=state, statement_type=statement_type, reused=reuse)
        if modality == "on_demand" else 0,
        duration_ms=int(stats.get("EngineExecutionTimeInMillis") or stats.get("TotalExecutionTimeInMillis") or 0),
        planning_ms=int(stats.get("QueryPlanningTimeInMillis") or 0),
        reused=reuse,
        reuse_eligible=result_reuse_eligible(sql, statement_type, modality),
        modality=modality,
        **ast_facts(sql),
    )


def resolve_modality(qe: dict, workgroup: dict) -> str:
    engine = str((qe.get("EngineVersion") or {}).get("SelectedEngineVersion") or "").lower()
    configuration = workgroup.get("Configuration", {})
    if "spark" in engine:
        return "spark"
    catalog = str((qe.get("QueryExecutionContext") or {}).get("Catalog") or "").lower()
    if (
        qe.get("SubstatementType") == "FEDERATED"
        or "federated" in engine
        or "lambda" in catalog
    ):
        return "federated"
    if any(
        configuration.get(key)
        for key in (
            "CapacityReservation",
            "CapacityReservationName",
            "CapacityReservationConfiguration",
        )
    ):
        return "provisioned"
    return "on_demand"


def ast_facts(sql: str) -> dict[str, Any]:
    if sqlglot is None:
        return {
            "reads_tables": [],
            "selects_star": bool(re.search(r"\bselect\s+\*", sql, re.I)),
            "has_where": bool(re.search(r"\bwhere\b", sql, re.I)),
        }
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return {
            "reads_tables": [],
            "selects_star": bool(re.search(r"\bselect\s+\*", sql, re.I)),
            "has_where": bool(re.search(r"\bwhere\b", sql, re.I)),
        }
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = []
    for table in tree.find_all(exp.Table):
        if table.name.lower() not in ctes:
            full = ".".join(part for part in (table.catalog, table.db, table.name) if part)
            tables.append(full)
    return {
        "reads_tables": list(dict.fromkeys(tables)),
        "selects_star": any(
            isinstance(projection, exp.Star)
            or (
                isinstance(projection, exp.Column)
                and isinstance(projection.this, exp.Star)
            )
            for select in tree.find_all(exp.Select)
            for projection in select.expressions
        ),
        "has_where": any(True for _ in tree.find_all(exp.Where)),
    }


def result_reuse_eligible(sql: str, statement_type: str, modality: str) -> bool:
    """Gate conservador para não sugerir cache quando a AWS não o suporta."""
    if sqlglot is None or modality != "on_demand" or statement_type != "DML":
        return False
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return False
    if not any(True for _ in tree.find_all(exp.Select)):
        return False
    if len(list(tree.find_all(exp.Table))) > 20:
        return False
    for function in tree.find_all(exp.Func):
        name = str(function.sql_name() or function.name or "").lower()
        if name in _NONDETERMINISTIC_FUNCTIONS:
            return False
    return True
