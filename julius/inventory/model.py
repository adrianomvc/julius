"""Modelo interno normalizado por ativo, compartilhado por ingestão e coleta."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.config import (
    ANALYSIS_WINDOW_DAYS,
    DAYS_PER_MONTH,
    DPU_PER_WORKER,
    UNATTRIBUTED_GLUE_BUCKETS,
)


def _monthly_factor(window_days: int) -> float:
    """Converte uma medição da janela em número por mês.

    A coleta mede N dias completos; o relatório fala em mês. A conversão é
    explícita e mora só aqui — 30 dias não são um mês.

    O parâmetro é o tamanho da **janela**, nunca a vida do ativo: uma sessão
    que existiu dois dias mede dois dias de consumo, e projetar esses dois dias
    para um mês seria a extrapolação que este contrato existe para eliminar.
    """
    return DAYS_PER_MONTH / max(1, window_days or ANALYSIS_WINDOW_DAYS)


@dataclass
class ServiceCost:
    """Custo de cobrança por serviço (Cost Explorer — reconciliação)."""

    name: str
    monthly_cost: float
    subtitle: str = ""
    currency: str = "USD"
    period_start: str = ""
    data_through: str = ""
    estimated: bool = True
    period_kind: str = "month_to_date"
    cost_basis: str = "cost_explorer_unblended"
    forecast_cost_eom: float | None = None


@dataclass
class GlueJob:
    name: str
    glue_version: str = "3.0"
    job_mode: str = "SCRIPT"
    command_type: str = "glueetl"
    worker_type: str | None = None
    number_of_workers: int | None = None
    max_capacity: float | None = None
    auto_scaling: bool = False
    execution_class: str = "STANDARD"
    job_bookmark: bool = False
    timeout_min: int = 2880
    avg_execution_sec: float = 0.0
    p50_execution_sec: float = 0.0
    p95_execution_sec: float = 0.0
    max_execution_sec: float = 0.0
    execution_stddev_sec: float = 0.0
    avg_cpu_load: float | None = None
    avg_worker_utilization: float | None = None
    max_memory_used_pct: float | None = None
    max_disk_used_pct: float | None = None
    max_task_skew: float | None = None
    avg_all_executors: float | None = None
    avg_max_needed_executors: float | None = None
    shuffle_spill_bytes: float | None = None
    shuffle_read_bytes: float | None = None
    shuffle_write_bytes: float | None = None
    has_spill_evidence: bool = False
    spark_event_log_objects_scanned: int = 0
    spark_event_log_evidence_complete: bool = False
    spark_event_logs_path: str | None = None
    incremental_source_evidence: bool = False
    dpu_seconds_window: float = 0.0
    estimated_dpu_hours_window: float = 0.0
    runs_in_window: int = 0
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    # Custo alocado por rateio da cobrança real do bucket no Cost Explorer.
    # Nunca é fatura por job: o CE não expõe dimensão de recurso para Glue.
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    trigger_names: list[str] = field(default_factory=list)
    run_ids_in_window: list[str] = field(default_factory=list)
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    script_location: str | None = None
    default_argument_keys: list[str] = field(default_factory=list)
    connection_names: list[str] = field(default_factory=list)
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
    def dpu_per_worker(self) -> float:
        return DPU_PER_WORKER.get(self.worker_type or "", 0.0)

    @property
    def configured_dpu(self) -> float:
        if self.max_capacity is not None:
            return max(0.0, self.max_capacity)
        return max(0, self.number_of_workers or 0) * self.dpu_per_worker

    @property
    def capacity_unit(self) -> str:
        return "M-DPU" if self.command_type == "glueray" else "DPU"

    @property
    def actual_dpu_hours_window(self) -> float:
        return max(0.0, self.dpu_seconds_window / 3600.0)

    @property
    def total_dpu_hours_window(self) -> float:
        return self.actual_dpu_hours_window + max(0.0, self.estimated_dpu_hours_window)

    @property
    def monthly_factor(self) -> float:
        return _monthly_factor(self.window_days)

    @property
    def modeled_window_dpu_hours(self) -> float:
        """Consumo por duração × capacidade, quando a AWS não reportou DPU."""
        return self.runs_in_window * self.configured_dpu * (
            self.avg_execution_sec / 3600.0
        )

    @property
    def window_dpu_hours(self) -> float:
        """DPU-hora **realizada** na janela. Medida, nunca extrapolada."""
        if self.total_dpu_hours_window > 0:
            return self.total_dpu_hours_window
        return self.modeled_window_dpu_hours

    @property
    def monthly_dpu_hours(self) -> float:
        """A mesma DPU-hora expressa por mês, para os modelos financeiros."""
        return self.window_dpu_hours * self.monthly_factor

    @property
    def runs_per_month(self) -> float:
        return round(self.runs_in_window * self.monthly_factor, 1)


@dataclass
class InteractiveSession:
    session_id: str
    dpu: float = 5
    worker_type: str | None = None
    number_of_workers: int | None = None
    max_capacity: float | None = None
    glue_version: str = ""
    idle_timeout_min: int = 2880
    status: str = "READY"
    idle_hours_per_day: float = 0.0
    active_days_per_month: int = 22
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    created_on: str = ""
    completed_on: str = ""
    execution_time_sec: float = 0.0
    dpu_seconds: float = 0.0
    dpu_seconds_window: float | None = None
    estimated_dpu_hours_window: float = 0.0
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    last_activity_at: str = ""
    activity_evidence: bool = False
    statement_ids: list[str] = field(default_factory=list)

    @property
    def actual_dpu_hours_window(self) -> float:
        seconds = (
            self.dpu_seconds
            if self.dpu_seconds_window is None
            else self.dpu_seconds_window
        )
        return max(0.0, seconds / 3600.0)

    @property
    def dpu_hours(self) -> float:
        return self.actual_dpu_hours_window + max(0.0, self.estimated_dpu_hours_window)

    @property
    def monthly_factor(self) -> float:
        return _monthly_factor(self.window_days)

    @property
    def monthly_dpu_hours(self) -> float:
        return self.dpu_hours * self.monthly_factor


@dataclass
class GlueCrawler:
    name: str
    state: str = "READY"
    last_crawl_status: str = ""
    last_crawl_started_at: str = ""
    last_error: str = ""
    schedule_expression: str = ""
    schedule_state: str = "NOT_SCHEDULED"
    database_name: str = ""
    median_runtime_sec: float = 0.0
    last_runtime_sec: float = 0.0
    tables_created: int = 0
    tables_updated: int = 0
    tables_deleted: int = 0
    runs_in_window: int = 0
    failures_in_window: int = 0
    dpu_hours_window: float = 0.0
    owner_tag: str | None = None
    crawl_ids_in_window: list[str] = field(default_factory=list)
    expected_runs_monthly: float | None = None
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    coverage_days: int = 0
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    recrawl_behavior: str = "CRAWL_EVERYTHING"

    @property
    def monthly_factor(self) -> float:
        return _monthly_factor(self.window_days)

    @property
    def monthly_dpu_hours(self) -> float:
        return self.dpu_hours_window * self.monthly_factor

    @property
    def expected_runs_in_window(self) -> float | None:
        """Execuções esperadas pelo agendamento **na janela**.

        Comparar execuções da janela com uma expectativa mensal media coisas
        diferentes; a expectativa é trazida para o mesmo período.
        """
        if self.expected_runs_monthly is None:
            return None
        return self.expected_runs_monthly / self.monthly_factor


@dataclass
class GlueTrigger:
    name: str
    trigger_type: str = "ON_DEMAND"
    state: str = ""
    schedule_expression: str = ""
    workflow_name: str = ""
    job_names: list[str] = field(default_factory=list)
    crawler_names: list[str] = field(default_factory=list)
    owner_tag: str | None = None
    expected_runs_monthly: float | None = None


@dataclass
class DataBrewJob:
    name: str
    job_type: str = "RECIPE"
    max_capacity: int = 5
    timeout_min: int = 2880
    max_retries: int = 0
    schedule_names: list[str] = field(default_factory=list)
    runs_in_window: int = 0
    failures_in_window: int = 0
    execution_hours_window: float = 0.0
    estimated_node_hours_window: float = 0.0
    owner_tag: str | None = None
    run_ids_in_window: list[str] = field(default_factory=list)
    expected_runs_monthly: float | None = None
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    coverage_days: int = 0
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"

    @property
    def monthly_factor(self) -> float:
        return _monthly_factor(self.window_days)

    @property
    def monthly_node_hours(self) -> float:
        return self.estimated_node_hours_window * self.monthly_factor

    @property
    def expected_runs_in_window(self) -> float | None:
        """Execuções esperadas pelo agendamento **na janela**."""
        if self.expected_runs_monthly is None:
            return None
        return self.expected_runs_monthly / self.monthly_factor


@dataclass
class ProcessCost:
    process_id: str
    process_name: str
    root_type: str
    owner: str | None = None
    owner_source: str = "desconhecido"
    owner_confidence: float = 0.0
    owner_event_time: str = ""
    owner_event_name: str = ""
    actual_cost_window: float = 0.0
    estimated_cost_window: float = 0.0
    actual_dpu_hours: float = 0.0
    estimated_dpu_hours: float = 0.0
    currency: str = "USD"
    window_start: str = ""
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    allocation_method: str = "direct"
    component_names: list[str] = field(default_factory=list)

    @property
    def total_cost_window(self) -> float:
        return self.actual_cost_window + self.estimated_cost_window

    @property
    def monthly_factor(self) -> float:
        return _monthly_factor(self.window_days)

    @property
    def monthly_cost(self) -> float:
        """Custo do processo por mês — realizado na janela, não projetado.

        Substitui a antiga projeção para fim de mês, que multiplicava um MTD
        de poucos dias e virava o teto de economia de todas as oportunidades
        do processo.
        """
        return self.total_cost_window * self.monthly_factor


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


@dataclass
class GlueCostCoverage:
    """Cobertura da alocação do custo Glue vindo do Cost Explorer.

    O Cost Explorer não expõe dimensão de recurso para Glue: a cobrança real
    chega por `USAGE_TYPE`. O custo por job é rateio dessa cobrança pelas
    DPU-horas coletadas, nunca fatura por job.
    """

    period_start: str = ""
    data_through: str = ""
    cost_metric: str = ""
    currency: str = "USD"
    net_cost: float | None = None
    buckets: dict[str, float] = field(default_factory=dict)
    unknown_usage_types: list[str] = field(default_factory=list)
    cost_quality: str = "unavailable"
    modeled_ratio: float | None = None
    allocation_version: str = ""
    gaps: list[str] = field(default_factory=list)

    @property
    def unattributed_cost(self) -> float:
        """Buckets que não são rateados a nenhum ativo coletado."""
        return round(
            sum(
                value
                for name, value in self.buckets.items()
                if name in UNATTRIBUTED_GLUE_BUCKETS
            ),
            2,
        )


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
    currency: str = "USD"
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
    expression: str = ""
    state: str = "ENABLED"
    expected_runs_monthly: float | None = None


@dataclass
class ActorEvent:
    """Evidência normalizada de autoria extraída do CloudTrail."""

    resource_type: str
    resource_name: str
    event_name: str
    event_time: str = ""
    source_identity: str | None = None
    user_arn: str | None = None
    identity_type: str = ""
    event_source: str = ""
    is_human: bool = False


@dataclass
class CollectionHealth:
    """Resultado sanitizado de uma fonte durante uma coleta read-only."""

    source: str
    status: str = "ok"  # ok | partial | unavailable | error
    required: bool = False
    affects_status: bool = True
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    collected: int = 0
    expected: int | None = None
    coverage: float | None = None
    data_through: str = ""
    error_category: str = ""
    impact: str = ""
    next_action: str = ""


@dataclass
class Account:
    """Inventário de uma conta Consumer."""

    account_id: str
    region: str = "sa-east-1"
    period: str = ""
    lookback_days: int = ANALYSIS_WINDOW_DAYS
    generated_at: str = ""
    # Janela de análise sob a qual a conta foi coletada. Persistida no dataset
    # para que a mesma medição possa ser reinterpretada depois.
    window_start: str = ""
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    collection_health: list[CollectionHealth] = field(default_factory=list)
    services: list[ServiceCost] = field(default_factory=list)
    glue_jobs: list[GlueJob] = field(default_factory=list)
    interactive_sessions: list[InteractiveSession] = field(default_factory=list)
    glue_crawlers: list[GlueCrawler] = field(default_factory=list)
    glue_triggers: list[GlueTrigger] = field(default_factory=list)
    databrew_jobs: list[DataBrewJob] = field(default_factory=list)
    process_costs: list[ProcessCost] = field(default_factory=list)
    athena_queries: list[AthenaQuery] = field(default_factory=list)
    state_machines: list[StateMachine] = field(default_factory=list)
    sagemaker_apps: list[SageMakerApp] = field(default_factory=list)
    sagemaker_endpoints: list[SageMakerEndpoint] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)
    actor_events: list[ActorEvent] = field(default_factory=list)
    producer_candidates: list[ProducerCandidate] = field(default_factory=list)
    previous_results: list[PreviousResult] = field(default_factory=list)
    currency: str = "USD"
    athena_coverage: AthenaCoverage | None = None
    athena_actor_usage: list[AthenaActorUsage] = field(default_factory=list)
    glue_cost_coverage: GlueCostCoverage | None = None

    def job_by_name(self, name: str | None) -> GlueJob | None:
        if not name:
            return None
        return next((j for j in self.glue_jobs if j.name == name), None)

    @property
    def billing_cost_mtd(self) -> float:
        """Cobrança do mês-calendário até a data — o painel de fatura.

        Não é o período da análise. Nenhum baseline de oportunidade se apoia
        neste número; ele existe para reconciliar com o que a AWS emite.
        """
        return sum(s.monthly_cost for s in self.services)

    @property
    def total_monthly_cost(self) -> float:
        return self.billing_cost_mtd

    def process_cost_for_asset(self, asset_name: str) -> float | None:
        matches = [
            p.total_cost_window
            for p in self.process_costs
            if asset_name in p.component_names or p.process_name == asset_name
        ]
        return sum(matches) if matches else None

    def process_monthly_cost_for_asset(self, asset_name: str) -> float | None:
        matches = [
            p.monthly_cost
            for p in self.process_costs
            if asset_name in p.component_names or p.process_name == asset_name
        ]
        return sum(matches) if matches else None

    @property
    def collection_status(self) -> str:
        if not self.collection_health:
            return "not_reported"
        if any(
            item.required and item.status in {"error", "unavailable"}
            for item in self.collection_health
        ):
            return "failed"
        if any(
            item.affects_status and item.status != "ok"
            for item in self.collection_health
        ):
            return "partial"
        return "ok"
