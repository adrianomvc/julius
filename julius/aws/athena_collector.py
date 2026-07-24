"""Coleta mensal read-only e análise agregada do Athena.

Execuções e SQL original existem somente em memória. A saída contém padrões
sanitizados, cobertura, reconciliação e uso agregado por ator.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from julius.inventory.model import AthenaActorUsage, AthenaCoverage, AthenaQuery

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - instalação incompleta tem fallback seguro
    sqlglot = None
    exp = None

_MB = 1024**2
_GB = 1024**3
_MIN_BILLED = 10 * _MB
_COLUMNAR = {"PARQUET", "ORC"}
_ROW_FORMATS = {"CSV", "JSON", "TSV", "TEXT"}
_COMPRESSED_SUFFIXES = (
    ".gz", ".gzip", ".bz2", ".bzip2", ".snappy", ".zst", ".zstd", ".lz4"
)
_WIDE_TABLE_COLUMNS = 50
_PARTITION_PROJECTION_MIN_PARTITIONS = 1000
_DDL = {"DDL", "CREATE", "DROP", "ALTER", "MSCK", "SHOW", "DESCRIBE", "EXPLAIN"}
_AUTOMATION = re.compile(r"(service|automation|pipeline|scheduler|airflow|lambda|glue|states)", re.I)
_LITERAL_FALLBACK = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b(?:\d+(?:\.\d+)?|true|false|null)\b",
    re.I,
)
_WS = re.compile(r"\s+")


@dataclass
class AthenaExecutionEvidence:
    """Evidência efêmera. Nunca deve ser serializada ou persistida."""

    query_execution_id: str
    workgroup: str
    submitted_at: datetime
    state: str
    statement_type: str
    raw_sql: str = field(repr=False)
    exact_fingerprint: str = ""
    structural_fingerprint: str = ""
    sanitized_sql: str = ""
    parse_succeeded: bool = True
    scanned_bytes: int = 0
    billed_bytes: int = 0
    duration_ms: int = 0
    reused: bool = False
    modality: str = "on_demand"
    actor: str = "desconhecido"
    actor_type: str = "unknown"
    identity_source: str = "unknown"
    identity_confidence: str = "low"
    actor_email: str | None = None
    reads_tables: list[str] = field(default_factory=list)
    selects_star: bool = False
    partition_keys: list[str] = field(default_factory=list)
    missing_partition_filters: list[str] = field(default_factory=list)
    storage_formats: list[str] = field(default_factory=list)
    small_files_confirmed: bool = False
    small_file_count: int = 0
    average_file_bytes: int = 0
    total_table_bytes: int = 0
    max_table_columns: int = 0
    wide_tables: list[str] = field(default_factory=list)
    unpartitioned_tables: list[str] = field(default_factory=list)
    has_where: bool = False
    row_format_uncompressed: list[str] = field(default_factory=list)
    columnar_uncompressed: list[str] = field(default_factory=list)
    compression_codecs: list[str] = field(default_factory=list)
    partition_projection_enabled: bool = False
    partition_projection_candidates: list[str] = field(default_factory=list)
    partition_count: int = 0
    planning_ms: int = 0
    allocated_cost: float | None = None


@dataclass
class AthenaAnalysis:
    queries: list[AthenaQuery]
    actors: list[AthenaActorUsage]
    coverage: AthenaCoverage


def complete_utc_window(now: datetime | None = None, days: int = 30) -> tuple[datetime, datetime]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=days), end


def billable_bytes(
    scanned_bytes: int,
    *,
    state: str,
    statement_type: str = "",
    reused: bool = False,
) -> int:
    """Aplica arredondamento por MB e mínimo de 10 MB para on-demand elegível."""
    if reused or state == "FAILED" or statement_type.upper() in _DDL or scanned_bytes <= 0:
        return 0
    # CANCELLED pode ser cobrada pelos bytes efetivamente processados.
    if state not in {"SUCCEEDED", "CANCELLED"}:
        return 0
    return max(_MIN_BILLED, math.ceil(scanned_bytes / _MB) * _MB)


def fingerprints(sql: str) -> tuple[str, str, str, bool]:
    """Retorna fingerprint exato, estrutural, SQL sanitizado e sucesso do AST."""
    parsed = None
    if sqlglot is not None:
        try:
            parsed = sqlglot.parse_one(sql, read="athena")
        except Exception:
            try:
                parsed = sqlglot.parse_one(sql, read="trino")
            except Exception:
                parsed = None
    if parsed is not None:
        exact_text = parsed.sql(dialect="athena", pretty=False, normalize=True)
        structural_ast = parsed.copy().transform(
            lambda node: exp.Placeholder() if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)) else node
        )
        structural_text = structural_ast.sql(dialect="athena", pretty=False, normalize=True)
        sanitized = structural_text
        ok = True
    else:
        exact_text = _WS.sub(" ", sql).strip().lower()
        structural_text = _WS.sub(" ", _LITERAL_FALLBACK.sub("?", sql)).strip().lower()
        sanitized = structural_text
        ok = False
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return digest(exact_text), digest(structural_text), sanitized[:500], ok


def recurrence(timestamps: Iterable[datetime]) -> tuple[bool, bool, bool]:
    stamps = sorted(timestamps)
    days = {stamp.date() for stamp in stamps}
    recurring = len(stamps) >= 3 and len(days) >= 2
    burst = any(
        (stamps[index + 2] - stamps[index]) <= timedelta(minutes=60)
        for index in range(max(0, len(stamps) - 2))
    )
    regular = False
    if len(stamps) >= 4 and len(days) >= 3:
        intervals = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
        median = statistics.median(intervals)
        near = sum(abs(value - median) <= median * 0.2 for value in intervals)
        regular = bool(median and near / len(intervals) >= 0.6)
    return recurring, burst, regular


def resolve_actor(event: dict[str, Any] | None) -> tuple[str, str, str, str, str | None]:
    """Resolve ator sem expor o evento ou identificadores sensíveis."""
    if not event:
        return "desconhecido", "unknown", "unknown", "low", None
    identity = event.get("userIdentity") or {}
    session = identity.get("sessionContext") or {}
    attrs = session.get("attributes") or {}
    on_behalf = identity.get("onBehalfOf") or session.get("onBehalfOf") or {}
    if on_behalf.get("userId"):
        return str(on_behalf["userId"]), "human", "identity_center", "high", None
    source = identity.get("sourceIdentity") or session.get("sourceIdentity") or attrs.get("sourceIdentity")
    if source:
        return str(source), "human", "source_identity", "high", None
    if identity.get("type") == "IAMUser":
        return str(identity.get("userName") or _arn_tail(identity.get("arn"))), "human", "iam_user", "high", None
    if identity.get("type") == "AssumedRole":
        raw_identity = identity.get("arn") or identity.get("principalId")
        name = _arn_tail(raw_identity)
        actor_type = "automation" if _AUTOMATION.search(str(raw_identity or "")) else "role_session"
        return name, actor_type, "assumed_role", "medium", None
    principal = identity.get("invokedBy") or identity.get("type")
    if principal:
        return str(principal), "automation", "service", "medium", None
    return "desconhecido", "unknown", "unknown", "low", None


def collect_analysis(
    athena_client,
    *,
    cloudwatch_client=None,
    cloudtrail_client=None,
    identitystore_client=None,
    glue_client=None,
    s3_client=None,
    ce_client=None,
    lookback_days: int = 30,
    max_ids_per_workgroup: int | None = None,
    now: datetime | None = None,
) -> AthenaAnalysis:
    start, end = complete_utc_window(now, lookback_days)
    coverage = AthenaCoverage(window_start=start.isoformat(), window_end=end.isoformat())
    workgroups, configs = _workgroups(athena_client, coverage)
    evidence: list[AthenaExecutionEvidence] = []

    for workgroup in workgroups:
        ids, truncated = _execution_ids(
            athena_client, workgroup, max_ids=max_ids_per_workgroup, gaps=coverage.gaps
        )
        coverage.truncated = coverage.truncated or truncated
        if ids is None:
            continue
        coverage.workgroups_covered += 1
        for qe in _query_executions(athena_client, ids, coverage.gaps):
            item = _execution(qe, configs.get(workgroup, {}))
            if item and start <= item.submitted_at < end:
                evidence.append(item)

    if evidence:
        coverage.oldest_submission = min(item.submitted_at for item in evidence).isoformat()
    _enrich_catalog(evidence, glue_client, s3_client, coverage.gaps)
    _enrich_actors(evidence, cloudtrail_client, identitystore_client, start, end, coverage.gaps)
    coverage.api_scanned_bytes = sum(
        item.scanned_bytes for item in evidence
        if item.modality == "on_demand" and item.statement_type not in _DDL
    )
    coverage.api_billed_bytes = sum(item.billed_bytes for item in evidence if item.modality == "on_demand")
    _reconcile_cloudwatch(coverage, cloudwatch_client, workgroups, start, end)
    daily_cost, metric, currency, isolated = _costs(ce_client, start, end, coverage.gaps)
    coverage.cost_metric, coverage.currency = metric, currency
    coverage.net_cost = round(sum(daily_cost.values()), 6) if daily_cost else None
    allocation_complete = _allocate(
        evidence, daily_cost, isolated and _reconciled(coverage)
    )
    if isolated and _reconciled(coverage) and allocation_complete:
        coverage.cost_quality = "reconciled"
    elif daily_cost:
        coverage.cost_quality = "partial"
    queries = _aggregate_queries(evidence, coverage)
    actors = _aggregate_actors(evidence, queries, coverage)
    return AthenaAnalysis(queries=queries, actors=actors, coverage=coverage)


def collect_queries(
    athena_client, *, lookback_days: int = 30, max_ids: int | None = None, now: datetime | None = None
) -> list[AthenaQuery]:
    """Compatibilidade: inventários antigos esperam apenas a lista de queries."""
    return collect_analysis(
        athena_client, lookback_days=lookback_days, max_ids_per_workgroup=max_ids, now=now
    ).queries


def _workgroups(client, coverage: AthenaCoverage) -> tuple[list[str], dict[str, dict]]:
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


def _execution_ids(client, workgroup: str, *, max_ids: int | None, gaps: list[str]):
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


def _query_executions(client, ids: list[str], gaps: list[str]):
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


def _execution(qe: dict, workgroup: dict) -> AthenaExecutionEvidence | None:
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
    modality = _modality(qe, workgroup)
    return AthenaExecutionEvidence(
        query_execution_id=str(qe.get("QueryExecutionId") or ""),
        workgroup=str(qe.get("WorkGroup") or workgroup.get("Name") or "primary"),
        submitted_at=submitted,
        state=state,
        statement_type=statement_type,
        raw_sql=str(qe.get("Query") or ""),
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
        modality=modality,
        **_ast_facts(str(qe.get("Query") or "")),
    )


def _modality(qe: dict, workgroup: dict) -> str:
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


def _ast_facts(sql: str) -> dict[str, Any]:
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


def _enrich_catalog(items: list[AthenaExecutionEvidence], glue, s3, gaps: list[str]) -> None:
    if glue is None:
        return
    cache: dict[str, dict[str, Any]] = {}
    for item in items:
        for name in item.reads_tables:
            if name not in cache:
                parts = name.split(".")
                if len(parts) < 2:
                    continue
                try:
                    table = glue.get_table(DatabaseName=parts[-2], Name=parts[-1])["Table"]
                    keys = [key["Name"] for key in table.get("PartitionKeys", [])]
                    descriptor = table.get("StorageDescriptor") or {}
                    parameters = table.get("Parameters") or {}
                    fmt = _storage_format(descriptor, parameters)
                    objects = _object_evidence(s3, descriptor.get("Location"))
                    projection = str(parameters.get("projection.enabled") or "").lower() == "true"
                    partition_count = (
                        0 if projection else _partition_count(glue, parts[-2], parts[-1])
                    )
                    codecs, explicitly_uncompressed = _compression_evidence(
                        descriptor, parameters
                    )
                    cache[name] = {
                        "keys": keys,
                        "format": fmt,
                        "objects": objects,
                        "columns": len(descriptor.get("Columns") or []),
                        "projection": projection,
                        "partition_count": partition_count,
                        "codecs": codecs,
                        "columnar_uncompressed": (
                            fmt in _COLUMNAR and explicitly_uncompressed
                        ),
                        "row_uncompressed": (
                            fmt in _ROW_FORMATS
                            and objects["count"] > 0
                            and objects["compressed_count"] == 0
                        ),
                    }
                except Exception as exc:
                    gaps.append(f"Glue {name}: {type(exc).__name__}")
                    continue
            evidence = cache[name]
            keys = evidence["keys"]
            fmt = evidence["format"]
            objects = evidence["objects"]
            item.partition_keys.extend(key for key in keys if key not in item.partition_keys)
            if fmt and fmt not in item.storage_formats:
                item.storage_formats.append(fmt)
            if objects["small_files"]:
                item.small_files_confirmed = True
                item.small_file_count = max(item.small_file_count, objects["count"])
                item.average_file_bytes = objects["average"]
            item.total_table_bytes += objects["total"]
            item.max_table_columns = max(item.max_table_columns, evidence["columns"])
            if evidence["columns"] >= _WIDE_TABLE_COLUMNS:
                item.wide_tables.append(name)
            if not keys:
                item.unpartitioned_tables.append(name)
            if evidence["row_uncompressed"]:
                item.row_format_uncompressed.append(name)
            if evidence["columnar_uncompressed"]:
                item.columnar_uncompressed.append(name)
            item.compression_codecs.extend(evidence["codecs"])
            item.partition_projection_enabled = (
                item.partition_projection_enabled or evidence["projection"]
            )
            item.partition_count = max(item.partition_count, evidence["partition_count"])
            if (
                keys
                and not evidence["projection"]
                and evidence["partition_count"] >= _PARTITION_PROJECTION_MIN_PARTITIONS
            ):
                item.partition_projection_candidates.append(name)
            for key in keys:
                if not _has_partition_predicate(item.raw_sql, name, key):
                    item.missing_partition_filters.append(key)
        item.missing_partition_filters = sorted(set(item.missing_partition_filters))
        item.wide_tables = sorted(set(item.wide_tables))
        item.unpartitioned_tables = sorted(set(item.unpartitioned_tables))
        item.row_format_uncompressed = sorted(set(item.row_format_uncompressed))
        item.columnar_uncompressed = sorted(set(item.columnar_uncompressed))
        item.compression_codecs = sorted(set(item.compression_codecs))
        item.partition_projection_candidates = sorted(
            set(item.partition_projection_candidates)
        )


def _missing_partition_predicates(sql: str, keys: list[str]) -> list[str]:
    if sqlglot is None:
        return list(keys)
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return list(keys)
    filtered: set[str] = set()
    for where in tree.find_all(exp.Where):
        for column in where.find_all(exp.Column):
            filtered.add(column.name.lower())
    return [key for key in keys if key.lower() not in filtered]


def _has_partition_predicate(sql: str, table_name: str, key: str) -> bool:
    """Confirma o predicado no alias da tabela, inclusive dentro de CTEs."""
    if sqlglot is None:
        return False
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return False
    target = table_name.split(".")[-1].lower()
    physical_tables = list(tree.find_all(exp.Table))
    aliases = {
        table.alias_or_name.lower()
        for table in physical_tables
        if table.name.lower() == target
    }
    for where in tree.find_all(exp.Where):
        for column in where.find_all(exp.Column):
            if column.name.lower() != key.lower():
                continue
            qualifier = (column.table or "").lower()
            if qualifier in aliases:
                return True
            if not qualifier and len(physical_tables) == 1:
                return True
    return False


def _storage_format(descriptor: dict, parameters: dict | None = None) -> str:
    value = " ".join(
        str(descriptor.get(key) or "") for key in ("InputFormat", "OutputFormat")
    )
    serde = descriptor.get("SerdeInfo") or {}
    value += " " + str(serde.get("SerializationLibrary") or "")
    classification = str((parameters or {}).get("classification") or "")
    value = (value + " " + classification).upper()
    aliases = {
        "PARQUET": ("PARQUET",),
        "ORC": ("ORC",),
        "JSON": ("JSON",),
        "CSV": ("CSV", "OPENCSV"),
        "TSV": ("TSV",),
        "TEXT": ("TEXTINPUT", "LAZYSIMPLE"),
    }
    return next(
        (fmt for fmt, markers in aliases.items() if any(marker in value for marker in markers)),
        "",
    )


def _small_file_evidence(s3, location: str | None) -> tuple[int, int, bool]:
    evidence = _object_evidence(s3, location)
    return evidence["count"], evidence["average"], evidence["small_files"]


def _object_evidence(s3, location: str | None) -> dict[str, Any]:
    """Agrega tamanhos/extensões; nunca persiste chaves ou conteúdo S3."""
    empty = {
        "count": 0,
        "average": 0,
        "total": 0,
        "small_files": False,
        "compressed_count": 0,
    }
    if s3 is None or not location or not location.startswith("s3://"):
        return empty
    bucket, _, prefix = location[5:].partition("/")
    sizes: list[int] = []
    compressed_count = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                size = int(obj.get("Size") or 0)
                if size <= 0:
                    continue
                sizes.append(size)
                key = str(obj.get("Key") or "").lower()
                compressed_count += key.endswith(_COMPRESSED_SUFFIXES)
    except Exception:
        return empty
    if not sizes:
        return empty
    average = round(sum(sizes) / len(sizes))
    return {
        "count": len(sizes),
        "average": average,
        "total": sum(sizes),
        "small_files": len(sizes) >= 100 and average < 64 * _MB,
        "compressed_count": compressed_count,
    }


def _compression_evidence(
    descriptor: dict, parameters: dict
) -> tuple[list[str], bool]:
    serde_parameters = (descriptor.get("SerdeInfo") or {}).get("Parameters") or {}
    combined = {**parameters, **serde_parameters}
    values = [
        str(value).upper()
        for key, value in combined.items()
        if "compress" in str(key).lower()
    ]
    codecs = sorted(
        {
            codec
            for codec in ("GZIP", "SNAPPY", "ZSTD", "ZLIB", "BZIP2", "LZ4")
            if any(codec in value for value in values)
        }
    )
    explicitly_uncompressed = (
        descriptor.get("Compressed") is False
        and any(value in {"NONE", "UNCOMPRESSED"} for value in values)
    )
    return codecs, explicitly_uncompressed


def _partition_count(glue, database: str, table: str) -> int:
    try:
        paginator = glue.get_paginator("get_partitions")
        return sum(
            len(page.get("Partitions", []))
            for page in paginator.paginate(
                DatabaseName=database,
                TableName=table,
                ExcludeColumnSchema=True,
            )
        )
    except Exception:
        return 0


def _enrich_actors(items, cloudtrail, identitystore, start, end, gaps):
    if cloudtrail is None:
        return
    by_id = {item.query_execution_id: item for item in items}
    try:
        paginator = cloudtrail.get_paginator("lookup_events")
        pages = paginator.paginate(
            LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "StartQueryExecution"}],
            StartTime=start,
            EndTime=end,
        )
        for page in pages:
            for wrapper in page.get("Events", []):
                try:
                    event = json.loads(wrapper.get("CloudTrailEvent") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                query_id = ((event.get("responseElements") or {}).get("queryExecutionId"))
                item = by_id.get(query_id)
                if item is None:
                    continue
                actor, kind, source, confidence, email = resolve_actor(event)
                if source == "identity_center" and identitystore is not None:
                    actor, email, confidence = _describe_identity(
                        identitystore, event, actor, gaps
                    )
                item.actor, item.actor_type = actor, kind
                item.identity_source, item.identity_confidence = source, confidence
                item.actor_email = email
    except Exception as exc:
        gaps.append(f"CloudTrail: {type(exc).__name__}")


def _describe_identity(client, event, user_id, gaps):
    identity = event.get("userIdentity") or {}
    session = identity.get("sessionContext") or {}
    on_behalf = identity.get("onBehalfOf") or session.get("onBehalfOf") or {}
    store = on_behalf.get("identityStoreId")
    arn = on_behalf.get("identityStoreArn") or ""
    if not store and ":identitystore/" in arn:
        store = arn.rsplit("/", 1)[-1]
    if not store:
        return user_id, None, "medium"
    try:
        user = client.describe_user(IdentityStoreId=store, UserId=user_id)
        name = user.get("DisplayName") or user.get("UserName") or user_id
        emails = user.get("Emails") or []
        email = next((entry.get("Value") for entry in emails if entry.get("Primary")), None)
        return str(name), email, "high"
    except Exception as exc:
        gaps.append(f"IdentityStore: {type(exc).__name__}")
        return user_id, None, "medium"


def _reconcile_cloudwatch(coverage, client, workgroups, start, end):
    if client is None:
        coverage.gaps.append("CloudWatch não coletado")
        return
    total = 0.0
    complete = True
    for workgroup in workgroups:
        try:
            response = client.get_metric_statistics(
                Namespace="AWS/Athena",
                MetricName="ProcessedBytes",
                Dimensions=[{"Name": "WorkGroup", "Value": workgroup}],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Sum"],
            )
            total += sum(float(point.get("Sum") or 0) for point in response.get("Datapoints", []))
        except Exception as exc:
            complete = False
            coverage.gaps.append(f"CloudWatch {workgroup}: {type(exc).__name__}")
    if complete:
        coverage.cloudwatch_bytes = int(total)
        if coverage.api_scanned_bytes:
            coverage.reconciliation_ratio = round(total / coverage.api_scanned_bytes, 4)


def _costs(client, start, end, gaps):
    if client is None:
        gaps.append("Cost Explorer não coletado")
        return {}, "", "BRL", False
    for metric in ("NetUnblendedCost", "UnblendedCost"):
        try:
            response = client.get_cost_and_usage(
                TimePeriod={"Start": start.date().isoformat(), "End": end.date().isoformat()},
                Granularity="DAILY",
                Metrics=[metric],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Athena"]}},
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
            daily: dict[str, float] = {}
            currency = "USD"
            isolated = True
            for period in response.get("ResultsByTime", []):
                amount = 0.0
                for group in period.get("Groups", []):
                    usage = " ".join(group.get("Keys", [])).lower()
                    value = (group.get("Metrics") or {}).get(metric, {})
                    if any(term in usage for term in ("dpu", "capacity", "spark")):
                        continue
                    if usage and not any(term in usage for term in ("bytes", "tb", "data", "query")):
                        isolated = False
                    amount += float(value.get("Amount") or 0)
                    currency = value.get("Unit") or currency
                daily[period["TimePeriod"]["Start"]] = amount
            return daily, metric, currency, isolated
        except Exception as exc:
            gaps.append(f"Cost Explorer {metric}: {type(exc).__name__}")
    return {}, "", "BRL", False


def _reconciled(coverage: AthenaCoverage) -> bool:
    blocking = (
        "list_work_groups",
        "list_query_executions",
        "get_work_group",
        "execuções não processadas",
        "CloudWatch",
        "métricas CloudWatch desabilitadas",
    )
    return (
        coverage.workgroups_total > 0
        and coverage.workgroups_covered == coverage.workgroups_total
        and not coverage.truncated
        and not any(any(marker in gap for marker in blocking) for gap in coverage.gaps)
        and coverage.reconciliation_ratio is not None
        and 0.95 <= coverage.reconciliation_ratio <= 1.05
    )


def _allocate(items, daily_cost, allowed):
    if not allowed:
        return False
    by_day: dict[str, list[AthenaExecutionEvidence]] = defaultdict(list)
    for item in items:
        if item.modality == "on_demand" and item.billed_bytes:
            by_day[item.submitted_at.date().isoformat()].append(item)
    for day, executions in by_day.items():
        total = sum(item.billed_bytes for item in executions)
        cost = daily_cost.get(day)
        if total and cost is not None:
            for item in executions:
                item.allocated_cost = cost * item.billed_bytes / total
    return all(
        not amount or day in by_day
        for day, amount in daily_cost.items()
    )


def _aggregate_queries(items, coverage):
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
                p95_ms=_percentile(durations, 0.95),
                failed_runs=sum(i.state == "FAILED" for i in group),
                cancelled_runs=sum(i.state == "CANCELLED" for i in group),
                reused_runs=sum(i.reused for i in group),
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
                p95_planning_ms=_percentile(planning, 0.95),
                parse_succeeded=all(i.parse_succeeded for i in group),
                evidence=_query_evidence(group, missing),
            )
        )
    return sorted(output, key=lambda q: (q.allocated_cost or 0, q.billed_bytes), reverse=True)


def _aggregate_actors(items, queries, coverage):
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


def _query_evidence(group, missing):
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
    if not all(item.parse_succeeded for item in group):
        evidence.append("parsing AST incompleto; recomendações semânticas bloqueadas")
    return evidence


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    return int(values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)])


def _arn_tail(value: Any) -> str:
    text = str(value or "")
    return text.rsplit("/", 1)[-1].rsplit(":", 1)[-1] or "desconhecido"
