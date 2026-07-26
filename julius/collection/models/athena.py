"""Padrões de query, uso por ator e cobertura da coleta Athena."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.settings import ANALYSIS_WINDOW_DAYS


@dataclass
class AthenaQuery:
    query_id: str
    workgroup: str = "primary"
    statement: str = ""
    data_scanned_bytes: int = 0
    executions_per_month: int = 0
    has_partition_filter: bool = True
    table_is_partitioned: bool = False
    selects_star: bool = False
    result_reuse_enabled: bool = False
    bytes_scanned_cutoff: bool = False
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    reads_tables: list[str] = field(default_factory=list)
    # Agregado sanitizado de um padrão de query. `query_id` continua aceito
    # para preservar compatibilidade com inventários anteriores.
    exact_fingerprint: str = ""
    structural_fingerprint: str = ""
    modality: str = "on_demand"
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    currency: str = "USD"
    active_days: int = 0
    actor_count: int = 0
    actors: list[str] = field(default_factory=list)
    recurring: bool = False
    burst: bool = False
    regular: bool = False
    automated: bool = False
    p50_ms: int = 0
    p95_ms: int = 0
    failed_runs: int = 0
    cancelled_runs: int = 0
    reused_runs: int = 0
    reuse_eligible_runs: int = 0
    reuse_avoidable_billed_bytes: int = 0
    reuse_avoidable_cost: float | None = None
    billed_bytes: int = 0
    avg_billed_bytes: int = 0
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
    full_scan_confirmed: bool = False
    row_format_uncompressed: list[str] = field(default_factory=list)
    columnar_uncompressed: list[str] = field(default_factory=list)
    compression_codecs: list[str] = field(default_factory=list)
    partition_projection_enabled: bool = False
    partition_projection_candidates: list[str] = field(default_factory=list)
    partition_count: int = 0
    p95_planning_ms: int = 0
    parse_succeeded: bool = True
    evidence: list[str] = field(default_factory=list)
    opportunity_refs: list[str] = field(default_factory=list)

    @property
    def monthly_bytes_scanned(self) -> int:
        return self.billed_bytes or int(self.data_scanned_bytes * self.executions_per_month)


@dataclass
class AthenaActorUsage:
    """Visão agregada por ator; nunca substitui ownership da conta."""

    actor: str
    actor_type: str = "unknown"
    identity_source: str = "unknown"
    identity_confidence: str = "low"
    email: str | None = None
    query_count: int = 0
    allocated_cost: float | None = None
    currency: str = "USD"
    billed_bytes: int = 0
    active_days: int = 0
    recurring_patterns: int = 0
    bursts: int = 0
    selects_star: int = 0
    missing_partition_filters: int = 0
    full_scans: int = 0
    unpartitioned_tables: int = 0
    compression_findings: int = 0
    partition_projection_candidates: int = 0
    failures: int = 0
    automated: bool = False
    opportunity_refs: list[str] = field(default_factory=list)


@dataclass
class AthenaCoverage:
    """Cobertura e reconciliação da coleta do Athena na janela de análise."""

    window_start: str = ""
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    workgroups_total: int = 0
    workgroups_covered: int = 0
    workgroups: list[str] = field(default_factory=list)
    oldest_submission: str = ""
    truncated: bool = False
    api_scanned_bytes: int = 0
    api_billed_bytes: int = 0
    cloudwatch_bytes: int | None = None
    reconciliation_ratio: float | None = None
    cost_quality: str = "unavailable"
    cost_metric: str = ""
    net_cost: float | None = None
    currency: str = "USD"
    gaps: list[str] = field(default_factory=list)
