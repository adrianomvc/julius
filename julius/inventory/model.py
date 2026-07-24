"""Modelo interno normalizado por ativo, compartilhado por ingestão e coleta."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.config import DPU_PER_WORKER


@dataclass
class ServiceCost:
    """Custo mensal por serviço (Cost Explorer — reconciliação)."""

    name: str
    monthly_cost: float
    subtitle: str = ""
    currency: str = "BRL"


@dataclass
class GlueJob:
    name: str
    glue_version: str = "3.0"
    worker_type: str = "G.1X"
    number_of_workers: int = 10
    auto_scaling: bool = False
    execution_class: str = "STANDARD"
    job_bookmark: bool = False
    timeout_min: int = 2880
    runs_per_month: int = 0
    avg_execution_sec: float = 0.0
    avg_cpu_load: float | None = None
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    script_location: str | None = None
    # True/False = sensível a tempo (SLA); None = desconhecido. FLEX só p/ False.
    time_sensitive: bool | None = None
    # Confiabilidade: fração de execuções (incl. retries) que falham, e o compute
    # médio gasto até a falha (Glue cobra DPU-hora mesmo em execução que falha).
    failure_rate: float = 0.0
    avg_failed_execution_sec: float = 0.0
    max_retries: int = 0
    reads_tables: list[str] = field(default_factory=list)
    writes_tables: list[str] = field(default_factory=list)

    @property
    def glue_version_num(self) -> float:
        try:
            return float(self.glue_version)
        except (TypeError, ValueError):
            return 99.0

    @property
    def dpu_per_worker(self) -> int:
        return DPU_PER_WORKER.get(self.worker_type, 1)

    @property
    def monthly_dpu_hours(self) -> float:
        """DPU-hora/mês estimado a partir das execuções observadas."""
        hours = self.avg_execution_sec / 3600.0
        return self.runs_per_month * self.number_of_workers * self.dpu_per_worker * hours


@dataclass
class InteractiveSession:
    session_id: str
    dpu: int = 5
    idle_timeout_min: int = 2880
    status: str = "READY"
    idle_hours_per_day: float = 0.0
    active_days_per_month: int = 22
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None


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
    currency: str = "BRL"
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
    currency: str = "BRL"
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
    """Cobertura e reconciliação da coleta mensal do Athena."""

    window_start: str = ""
    window_end: str = ""
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
    currency: str = "BRL"
    gaps: list[str] = field(default_factory=list)


@dataclass
class ProducerCandidate:
    """Processo/produto candidato à camada Producer (governança, MVP 2).

    Dois scores independentes: `candidate` (assumiu perfil de produção?) e
    `readiness` (é viável migrar agora?). No MVP 1A vêm do dataset exportado.
    """

    name: str
    candidate_score: int = 0
    readiness_score: int = 0


@dataclass
class PreviousResult:
    """Recomendação já implementada: previsto × realizado (calibra estimativas)."""

    title: str
    asset: str = ""
    date: str = ""
    predicted_monthly: float = 0.0
    realized_monthly: float = 0.0
    unit: str = ""

    @property
    def precision(self) -> int:
        if self.predicted_monthly <= 0:
            return 0
        err = abs(self.realized_monthly - self.predicted_monthly) / self.predicted_monthly
        return int(max(0, min(100, round((1 - err) * 100))))


@dataclass
class StateMachine:
    """Step Functions state machine."""

    name: str
    type: str = "STANDARD"                 # STANDARD | EXPRESS
    executions_per_month: int = 0
    avg_state_transitions: int = 0
    avg_duration_sec: float = 0.0
    idempotent: bool | None = None
    has_polling_loop: bool = False
    poll_extra_transitions: int = 0        # transições extras por execução por polling
    max_retry_attempts: int = 0
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    glue_jobs: list[str] = field(default_factory=list)
    schedule_names: list[str] = field(default_factory=list)


@dataclass
class SageMakerApp:
    """App do SageMaker Studio / Notebook (JupyterLab, KernelGateway, Code Editor)."""

    name: str
    app_type: str = "JupyterLab"
    instance_type: str = "ml.t3.medium"
    status: str = "InService"
    idle_hours_per_day: float = 0.0
    idle_shutdown_min: int = 0             # 0 = desabilitado
    active_days_per_month: int = 22
    coverage_days: int = 0
    owner_tag: str | None = None


@dataclass
class SageMakerEndpoint:
    """Endpoint de inferência em tempo real."""

    name: str
    instance_type: str = "ml.m5.large"
    instance_count: int = 1
    invocations_per_month: int = 0
    auto_scaling: bool = False
    min_capacity: int = 1
    coverage_days: int = 0
    owner_tag: str | None = None


@dataclass
class Table:
    """Tabela do Glue Catalog gerada pela conta, com sinal de uso (toques).

    `touches_90d` vem da tabela oficial de toques; `written_by` da linhagem
    (qual job escreve a tabela). No MVP 1A/1B vêm do dataset exportado; no MVP 2
    vêm dos coletores (Athena/toques) e do grafo de processos.
    """

    name: str
    written_by: str | None = None       # nome do Glue Job que escreve a tabela
    touches_90d: int = 0                 # acessos na janela (tabela oficial de toques)
    consuming_accounts: int = 0
    consuming_communities: int = 0
    storage_bytes: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    temporary: bool = False              # tabela temporária/staging não conta como órfã
    datawarm_published: bool = False     # publicada ao ecossistema via DataWarm
    used_by_accounts: list[str] = field(default_factory=list)
    corporate_owner: str | None = None
    datawarm_owner: str | None = None
    primary_community: str | None = None


@dataclass
class Schedule:
    """Agendamento EventBridge/cron e seu alvo direto."""

    name: str
    target_type: str = "state_machine"
    target_name: str = ""
    owner_tag: str | None = None


@dataclass
class ActorEvent:
    """Evidência normalizada de autoria extraída do CloudTrail."""

    resource_type: str
    resource_name: str
    event_name: str
    event_time: str = ""
    source_identity: str | None = None
    user_arn: str | None = None


@dataclass
class Account:
    """Inventário de uma conta Consumer."""

    account_id: str
    region: str = "sa-east-1"
    period: str = ""
    lookback_days: int = 90
    generated_at: str = ""
    services: list[ServiceCost] = field(default_factory=list)
    glue_jobs: list[GlueJob] = field(default_factory=list)
    interactive_sessions: list[InteractiveSession] = field(default_factory=list)
    athena_queries: list[AthenaQuery] = field(default_factory=list)
    state_machines: list[StateMachine] = field(default_factory=list)
    sagemaker_apps: list[SageMakerApp] = field(default_factory=list)
    sagemaker_endpoints: list[SageMakerEndpoint] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)
    actor_events: list[ActorEvent] = field(default_factory=list)
    producer_candidates: list[ProducerCandidate] = field(default_factory=list)
    previous_results: list[PreviousResult] = field(default_factory=list)
    currency: str = "BRL"
    athena_coverage: AthenaCoverage | None = None
    athena_actor_usage: list[AthenaActorUsage] = field(default_factory=list)

    def job_by_name(self, name: str | None) -> GlueJob | None:
        if not name:
            return None
        return next((j for j in self.glue_jobs if j.name == name), None)

    @property
    def total_monthly_cost(self) -> float:
        return sum(s.monthly_cost for s in self.services)
