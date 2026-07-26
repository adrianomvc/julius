"""Evidência efêmera de execução e os cálculos que só dependem dela.

Nada aqui toca a AWS: são o registro em memória de uma execução, o
arredondamento de bytes faturáveis, o fingerprint estrutural e a
classificação de recorrência."""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - instalação incompleta tem fallback seguro
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

_MB = 1024**2
_MIN_BILLED = 10 * _MB
_DDL = {"DDL", "CREATE", "DROP", "ALTER", "MSCK", "SHOW", "DESCRIBE", "EXPLAIN"}
_RESULT_REUSE_WINDOW = timedelta(minutes=60)
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
    reuse_eligible: bool = False
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
        intervals = [(b - a).total_seconds() for a, b in pairwise(stamps)]
        median = statistics.median(intervals)
        near = sum(abs(value - median) <= median * 0.2 for value in intervals)
        regular = bool(median and near / len(intervals) >= 0.6)
    return recurring, burst, regular


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    return int(values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)])


def arn_tail(value: Any) -> str:
    text = str(value or "")
    return text.rsplit("/", 1)[-1].rsplit(":", 1)[-1] or "desconhecido"
