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
    #: Quando este padrão rodou pela última vez, em ISO-8601. É o que permite
    #: derivar a última **leitura** de cada tabela em `reads_tables` — e, por
    #: ela, do prefixo S3 que a tabela ocupa. Vazio significa não medido: o S3
    #: não expõe último acesso por objeto, então sem isto a única data conhecida
    #: de um arquivo é a da última escrita.
    last_execution_at: str = ""
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
    reuse_configured_runs: int = 0
    reuse_max_age_minutes: int | None = None
    reuse_eligible_runs: int = 0
    reuse_avoidable_billed_bytes: int = 0
    reuse_avoidable_cost: float | None = None
    reuse_ineligible_reasons: list[str] = field(default_factory=list)
    billed_bytes: int = 0
    avg_billed_bytes: int = 0
    partition_keys: list[str] = field(default_factory=list)
    missing_partition_filters: list[str] = field(default_factory=list)
    filter_columns: list[str] = field(default_factory=list)
    partition_candidate_keys: list[str] = field(default_factory=list)
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
    # False quando os nomes vieram apenas da configuração/fallback. Nesse caso
    # `workgroups_total` é a quantidade conhecida, não o total provado da conta.
    workgroups_discovery_complete: bool = True
    configured_workgroups: list[str] = field(default_factory=list)
    workgroup_roles: dict[str, str] = field(default_factory=dict)
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
    # Configuração declarada de cada workgroup, lida de `GetWorkGroup` — que
    # já era chamado para resolver modalidade e nunca teve o resto lido.
    # `None` no limite significa não consultado; `0` significa sem limite
    # configurado, que é a informação que a regra precisa.
    workgroup_output_locations: dict[str, str] = field(default_factory=dict)
    workgroup_scan_cutoffs: dict[str, int | None] = field(default_factory=dict)
    #: Por workgroup, de onde vieram os IDs de execução: `listing` quando o
    #: `ListQueryExecutions` respondeu, `output_location` quando foi preciso
    #: recuperá-los dos resultados gravados no S3. O alcance dos dois não é o
    #: mesmo — o segundo só enxerga query que gravou resultado e ainda não foi
    #: apagada por lifecycle —, então ler cobertura sem saber a origem é
    #: comparar medições diferentes.
    execution_source: dict[str, str] = field(default_factory=dict)


@dataclass
class AthenaCapacityReservation:
    name: str
    status: str = ""
    target_dpus: int = 0
    allocated_dpus_p95: float | None = None
    consumed_dpus_p95: float | None = None
    consumed_dpu_hours: float | None = None
    query_queue_p95_ms: float | None = None
    planning_p95_ms: float | None = None
    engine_p95_ms: float | None = None
    idle_hours: float | None = None
    workgroups: list[str] = field(default_factory=list)
    coverage_days: int = 0
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"

    @property
    def utilization_p95(self) -> float | None:
        if not self.target_dpus or self.consumed_dpus_p95 is None:
            return None
        return self.consumed_dpus_p95 / self.target_dpus
