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
    arn: str = ""
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
    # Falso quando `DescribeStateMachine` foi negado: a máquina existe, mas a
    # definição não foi lida — não há como saber se há loop de polling nem quais
    # jobs Glue ela chama. Antes uma máquina negada derrubava a listagem toda.
    definition_available: bool = True
    # Falso quando `ListExecutions` foi negado: `executions_per_month` e
    # `avg_duration_sec` são zero por falta de leitura, não por falta de uso.
    execution_history_available: bool = True
    # Contrafactual opcional, preenchido apenas por benchmark externo aprovado.
    # O Julius nunca executa esse benchmark nem inventa memória a partir da ASL.
    express_benchmark_duration_ms: int | None = None
    express_benchmark_memory_mb: int | None = None
    failed_executions: int = 0
    timed_out_executions: int = 0
    aborted_executions: int = 0
    throttled_events: int = 0
    redriven_executions: int = 0
    open_executions_max: int = 0
    service_integration_failures: int = 0
    duration_p95_ms: float | None = None
    avg_failed_state_transitions: int | None = None
    avg_retry_transitions: int | None = None
    cw_failed_executions: int = 0
    cw_timed_out_executions: int = 0
    cw_aborted_executions: int = 0


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
    arn: str = ""
    domain_id: str = ""
    space_name: str = ""
    user_profile_name: str = ""
    activity_metrics_available: bool = False
    cpu_p95: float | None = None
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    cost_coverage_days: int | None = None
    consistent_scans: int = 1


@dataclass
class SageMakerSpace:
    """Storage persistente e apps associados a um Studio Space."""

    name: str
    domain_id: str = ""
    arn: str = ""
    status: str = ""
    sharing_type: str = ""
    owner_tag: str | None = None
    owner_user_profile: str = ""
    ebs_volume_size_gb: int = 0
    created_at: str = ""
    last_modified_at: str = ""
    app_count: int = 0
    active_app_count: int = 0
    coverage_days: int = 0
    allocated_storage_cost: float | None = None
    cost_quality: str = "unavailable"
    cost_coverage_days: int | None = None
    consistent_scans: int = 1


@dataclass
class SageMakerDomain:
    """Domain Studio e o EFS doméstico gerenciado associado."""

    domain_id: str
    name: str = ""
    arn: str = ""
    status: str = ""
    home_efs_file_system_id: str = ""
    created_at: str = ""
    last_modified_at: str = ""
    owner_tag: str | None = None
    space_count: int = 0
    active_app_count: int = 0
    efs_storage_bytes: float | None = None
    efs_total_io_bytes: float | None = None
    efs_read_io_bytes: float | None = None
    efs_write_io_bytes: float | None = None
    efs_client_connections: float | None = None
    coverage_days: int = 0
    allocated_storage_cost: float | None = None
    cost_quality: str = "unavailable"
    cost_coverage_days: int | None = None
    consistent_scans: int = 1


@dataclass
class SageMakerEndpoint:
    """Endpoint de inferência em tempo real."""

    name: str
    instance_type: str = "ml.m5.large"
    instance_count: int = 1
    # `None` = CloudWatch não consultado; `0` = consultado e sem invocação.
    # Enquanto os dois eram zero, um endpoint em produção numa coleta sem
    # CloudWatch era reportado como ocioso, com o custo 24/7 inteiro.
    invocations_per_month: int | None = None
    auto_scaling: bool = False
    min_capacity: int = 1
    coverage_days: int = 0
    owner_tag: str | None = None
    arn: str = ""
    status: str = ""
    endpoint_config_name: str = ""
    mode: str = "real_time"
    variants: list[SageMakerVariant] = field(default_factory=list)
    inference_components: list[SageMakerInferenceComponent] = field(
        default_factory=list
    )
    invocations: int | None = None
    model_errors: int | None = None
    invocation_4xx: int | None = None
    invocation_5xx: int | None = None
    model_latency_p95_us: float | None = None
    backlog_without_capacity: int | None = None
    last_invocation_at: str = ""
    serverless_memory_mb: int = 0
    provisioned_concurrency: int = 0
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    cost_coverage_days: int | None = None
    consistent_scans: int = 1


@dataclass
class SageMakerVariant:
    """Uma variante de endpoint, preservada em vez de usar somente a primeira."""

    name: str
    instance_type: str = ""
    current_instance_count: int = 0
    desired_instance_count: int = 0
    initial_instance_count: int = 0
    min_capacity: int = 0
    max_capacity: int = 0
    auto_scaling: bool = False
    scaling_policy_count: int = 0
    serverless_memory_mb: int = 0
    provisioned_concurrency: int = 0
    invocations: int | None = None
    cpu_p95: float | None = None
    gpu_p95: float | None = None
    memory_p95: float | None = None


@dataclass
class SageMakerInferenceComponent:
    """Capacidade implantada por inference component."""

    name: str
    variant_name: str = ""
    status: str = ""
    instance_type: str = ""
    current_copies: int = 0
    desired_copies: int = 0
    min_copies: int = 0
    max_copies: int = 0
    auto_scaling: bool = False
    cpu_p95: float | None = None
    gpu_p95: float | None = None
    memory_p95: float | None = None


@dataclass
class SageMakerNotebook:
    """Notebook Instance clássico.

    O plano de controle informa que está ligado, mas não oferece uma métrica
    oficial de idle equivalente à do Studio. Por isso este inventário produz
    sinal contextual, não economia automática.
    """

    name: str
    arn: str = ""
    status: str = ""
    instance_type: str = ""
    platform_identifier: str = ""
    lifecycle_config_name: str = ""
    created_at: str = ""
    last_modified_at: str = ""
    coverage_days: int = 0
    owner_tag: str | None = None
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    cost_coverage_days: int | None = None
    consistent_scans: int = 1


@dataclass
class SageMakerJob:
    """Training, Processing ou Batch Transform na mesma janela de análise."""

    name: str
    kind: str
    arn: str = ""
    status: str = ""
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    instance_type: str = ""
    instance_count: int = 0
    duration_seconds: float = 0.0
    billable_seconds: float | None = None
    training_seconds: float | None = None
    use_spot: bool = False
    checkpoint_configured: bool = False
    keep_alive_seconds: int = 0
    warm_pool_status: str = ""
    warm_pool_billable_seconds: float = 0.0
    warm_pool_reused: bool = False
    failure_category: str = ""
    pipeline_name: str = ""
    workload_fingerprint: str = ""
    workload_runs: int = 0
    low_utilization_runs: int = 0
    history_coverage_days: int = 0
    in_financial_window: bool = True
    cpu_p95: float | None = None
    gpu_p95: float | None = None
    memory_p95: float | None = None
    disk_p95: float | None = None
    detailed_metrics: bool = False
    coverage_days: int = 0
    owner_tag: str | None = None
    modeled_cost: float | None = None
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    #: Por que não há custo, quando não há. Distingue os três casos que
    #: produziam o mesmo silêncio: configuração de recurso não descrita, tarifa
    #: ausente para o tipo de instância, e job que existiu mas não acumulou tempo
    #: faturável. Sem isto, "custo zero" e "custo desconhecido" ficam iguais.
    cost_unavailable_reason: str = ""
    cost_coverage_days: int | None = None
    consistent_scans: int = 1

    @property
    def instance_hours(self) -> float:
        seconds = (
            self.billable_seconds
            if self.billable_seconds is not None
            else self.duration_seconds
        )
        return max(0.0, float(seconds)) * max(0, self.instance_count) / 3600.0


@dataclass
class SageMakerFeatureGroup:
    """Feature Store online/offline e sua capacidade declarada."""

    name: str
    arn: str = ""
    status: str = ""
    online_store: bool = False
    offline_store: bool = False
    storage_type: str = ""
    throughput_mode: str = ""
    provisioned_read_capacity: int = 0
    provisioned_write_capacity: int = 0
    max_consumed_read_capacity: float | None = None
    max_consumed_write_capacity: float | None = None
    consumed_read_request_units: float | None = None
    consumed_write_request_units: float | None = None
    throttled_requests: int | None = None
    server_errors: int | None = None
    ttl_seconds: int | None = None
    coverage_days: int = 0
    owner_tag: str | None = None
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    cost_coverage_days: int | None = None
    consistent_scans: int = 1


@dataclass
class SageMakerPipeline:
    """Resumo de execuções e passos de uma Model Building Pipeline."""

    name: str
    arn: str = ""
    status: str = ""
    executions: int = 0
    succeeded: int = 0
    failed: int = 0
    stopped: int = 0
    avg_duration_seconds: float | None = None
    job_names: list[str] = field(default_factory=list)
    coverage_days: int = 0
    owner_tag: str | None = None


@dataclass
class SageMakerMonitoringSchedule:
    """Model Monitor já configurado; o Julius nunca cria schedules."""

    name: str
    arn: str = ""
    status: str = ""
    monitoring_type: str = ""
    endpoint_name: str = ""
    last_execution_status: str = ""
    last_execution_time: str = ""
    failure_reason: str = ""
    coverage_days: int = 0
    owner_tag: str | None = None


@dataclass
class SageMakerInferenceRecommendation:
    """Resultado já produzido pelo Inference Recommender."""

    job_name: str
    status: str = ""
    model_name: str = ""
    endpoint_name: str = ""
    recommended_instance_type: str = ""
    initial_instance_count: int = 0
    max_invocations: float | None = None
    model_latency_ms: float | None = None
    cost_per_hour: float | None = None
    created_at: str = ""
    coverage_days: int = 0


@dataclass
class Table:
    """Tabela do Glue Catalog gerada pela conta, com sinal de uso (toques).

    `touches_90d` vem da tabela oficial de toques; `written_by` da linhagem
    (qual job escreve a tabela). No MVP 1A/1B vêm do dataset exportado; no MVP 2
    vêm dos coletores (Athena/toques) e do grafo de processos.
    """

    name: str
    written_by: str | None = None       # nome do Glue Job que escreve a tabela
    #: `StorageDescriptor.Location` do catálogo. É o que liga a tabela ao
    #: prefixo S3 que ela ocupa — sem isso o inventário de S3 não tem onde
    #: olhar, e a location já vem na mesma resposta do `GetTables`.
    location: str = ""
    # `None` = toques não medidos; `0` = medidos e ninguém tocou. Enquanto os
    # dois eram zero, "não olhamos" e "ninguém usa" eram a mesma frase — e a
    # segunda vira dinheiro no relatório. A fonte de toques é opcional, então o
    # caso não medido é o comum, não a exceção.
    touches_90d: int | None = None       # acessos na janela (tabela oficial de toques)
    #: Quando a tabela foi lida pela última vez, em ISO-8601. Vem do histórico
    #: de queries Athena ou da tabela oficial de toques — nunca do S3, que não
    #: expõe último acesso por objeto. Vazio = não medido, e nesse caso a única
    #: data conhecida dos arquivos é a da última **escrita**, que não diz se o
    #: dado é usado. É essa diferença que separa uma recomendação de classe de
    #: armazenamento com economia de uma que sai como pergunta.
    last_read_at: str = ""
    #: De onde veio `last_read_at`. `touches` e `catalog_read_history` são
    #: leitura observada — alguém consultou a tabela e há registro disso.
    #: `process_lineage` é inferência: um job que declara ler a tabela rodou
    #: naquele instante, então o dado é consumido — mas o job pode ler só uma
    #: partição, e a data não é de leitura da tabela inteira. A distinção decide
    #: o que a regra pode afirmar com ela.
    last_read_source: str = ""
    consuming_accounts: int | None = None
    consuming_communities: int | None = None
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
    resource_arn: str = ""
    node_type: str = ""
    node_count: int = 0
    status: str = "available"
    #: Serverless cobra RPU-hora; provisionado cobra nó-hora.
    base_rpu: int = 0
    max_rpu: int | None = None
    price_performance_target: str = ""
    serverless_usage_limits: list[str] = field(default_factory=list)
    advisor_recommendations: list[dict[str, str]] = field(default_factory=list)
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
    #: Compute rateado da cobrança real. `None` = sem cobrança classificada, e
    #: aí nenhuma regra quantifica economia. Só compute entra aqui: pausar o
    #: cluster não para de cobrar armazenamento.
    allocated_compute_cost: float | None = None
    cost_quality: str = "unavailable"
