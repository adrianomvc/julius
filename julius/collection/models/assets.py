"""Demais ativos e registros do inventário."""

from __future__ import annotations

from dataclasses import dataclass, field


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
