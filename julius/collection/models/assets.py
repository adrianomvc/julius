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
    # `None` = histórico não amostrado. Zero seria uma afirmação — a de que a
    # máquina não transiciona — e ela zera o baseline de um serviço cobrado
    # justamente por transição.
    avg_state_transitions: int | None = None
    avg_duration_sec: float = 0.0
    # Tolerar semântica at-least-once é propriedade da lógica de negócio, não da
    # config: fica `None` até a análise contextual julgar a ASL.
    idempotent: bool | None = None
    has_polling_loop: bool = False
    poll_extra_transitions: int | None = None  # transições extras por execução
    max_retry_attempts: int = 0
    observed_runs: int = 0
    coverage_days: int = 0
    sampled_executions: int = 0            # execuções lidas do histórico
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
    # `None` = não coletado; `0` = desligado de fato. Confundir os dois fazia a
    # regra tratar app bem configurado como se não tivesse idle shutdown.
    idle_shutdown_min: int | None = None
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
class RedshiftCluster:
    """Cluster provisionado ou workgroup Serverless.

    O que a coleta enxerga é o plano de controle e o CloudWatch. Histórico de
    query, skew de distribuição e tabelas frias vivem em `SVV_*`/`STL_*`, que
    exigem conexão de banco ou a Redshift Data API — nenhuma das duas cabe na
    coleta read-only por API de controle. Os campos correspondentes não existem
    aqui de propósito: é melhor não ter o dado do que ter um campo que sempre
    vale zero e parece medido.
    """

    name: str
    #: `provisioned` ou `serverless` — a cobrança e as regras diferem.
    kind: str = "provisioned"
    node_type: str = ""
    node_count: int = 0
    status: str = "available"
    #: Serverless cobra RPU-hora; provisionado cobra nó-hora.
    base_rpu: int = 0
    #: Pausa/retomada agendada existe? Cluster parado não cobra compute.
    paused: bool = False
    #: Concurrency scaling e elastic resize deixam rastro no plano de controle.
    concurrency_scaling: bool = False
    encrypted: bool = False
    created_at: str = ""
    #: CloudWatch, sobre a janela de análise.
    avg_cpu_load: float | None = None
    max_cpu_load: float | None = None
    avg_connections: float | None = None
    #: `None` significa não medido; zero significa medido e vazio.
    queries_in_window: int | None = None
    observed_days: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
