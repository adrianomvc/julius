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

    @property
    def monthly_bytes_scanned(self) -> int:
        return int(self.data_scanned_bytes * self.executions_per_month)


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

    def job_by_name(self, name: str | None) -> GlueJob | None:
        if not name:
            return None
        return next((j for j in self.glue_jobs if j.name == name), None)

    @property
    def total_monthly_cost(self) -> float:
        return sum(s.monthly_cost for s in self.services)
