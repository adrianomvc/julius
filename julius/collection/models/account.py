"""A conta inteira, com tudo que foi coletado."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models.assets import (
    ActorEvent,
    PreviousResult,
    ProducerCandidate,
    RedshiftCluster,
    SageMakerApp,
    SageMakerDomain,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerInferenceRecommendation,
    SageMakerJob,
    SageMakerMonitoringSchedule,
    SageMakerNotebook,
    SageMakerPipeline,
    SageMakerSpace,
    Schedule,
    StateMachine,
    Table,
)
from julius.collection.models.athena import (
    AthenaActorUsage,
    AthenaCapacityReservation,
    AthenaCoverage,
    AthenaQuery,
)
from julius.collection.models.cost import (
    GlueCostCoverage,
    ProcessCost,
    RedshiftCostCoverage,
    SageMakerCostCoverage,
    SageMakerSavingsPlanCoverage,
    ServiceCost,
)
from julius.collection.models.glue import (
    DataBrewJob,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    GlueUsageProfile,
    InteractiveSession,
)
from julius.collection.models.health import CollectionHealth
from julius.collection.models.s3 import (
    S3Bucket,
    S3BucketConfig,
    S3CostCoverage,
    S3MultipartUpload,
    S3Prefix,
)
from julius.collection.settings import ANALYSIS_WINDOW_DAYS
from julius.collection.telemetry import RunTelemetry


@dataclass
class Account:
    """Inventário de uma conta Consumer."""

    account_id: str
    scope_profile: str = "full_analysis"
    s3_mode: str = "proposal"
    region: str = "sa-east-1"
    period: str = ""
    cadence: str = "weekly"
    financial_period: str = ""
    lookback_days: int = ANALYSIS_WINDOW_DAYS
    generated_at: str = ""
    # Identificador comum entre coleta, checkpoints e análise contextual.
    # Vazio mantém compatibilidade com datasets anteriores.
    scan_id: str = ""
    # Janela de análise sob a qual a conta foi coletada. Persistida no dataset
    # para que a mesma medição possa ser reinterpretada depois.
    window_start: str = ""
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    #: Primeira coleta desta conta, com janela profunda. Fica no dataset porque
    #: muda a leitura de tudo que depende de cobertura: uma cifra que apareceu no
    #: bootstrap não amadureceu ao longo de três coletas, ela nasceu madura.
    bootstrap: bool = False
    collection_health: list[CollectionHealth] = field(default_factory=list)
    run_telemetry: RunTelemetry = field(default_factory=RunTelemetry)
    services: list[ServiceCost] = field(default_factory=list)
    glue_jobs: list[GlueJob] = field(default_factory=list)
    interactive_sessions: list[InteractiveSession] = field(default_factory=list)
    glue_crawlers: list[GlueCrawler] = field(default_factory=list)
    glue_triggers: list[GlueTrigger] = field(default_factory=list)
    #: Guardrails do Glue. Não alimentam regra: são estado que o relatório
    #: mostra, porque prevenção não tem economia medida.
    glue_usage_profiles: list[GlueUsageProfile] = field(default_factory=list)
    databrew_jobs: list[DataBrewJob] = field(default_factory=list)
    process_costs: list[ProcessCost] = field(default_factory=list)
    athena_queries: list[AthenaQuery] = field(default_factory=list)
    athena_capacity_reservations: list[AthenaCapacityReservation] = field(
        default_factory=list
    )
    state_machines: list[StateMachine] = field(default_factory=list)
    stepfunctions_map_backlog: int = 0
    stepfunctions_open_executions: int = 0
    stepfunctions_service_integration_failures: int = 0
    stepfunctions_service_integration_timeouts: int = 0
    sagemaker_apps: list[SageMakerApp] = field(default_factory=list)
    sagemaker_spaces: list[SageMakerSpace] = field(default_factory=list)
    sagemaker_domains: list[SageMakerDomain] = field(default_factory=list)
    sagemaker_endpoints: list[SageMakerEndpoint] = field(default_factory=list)
    sagemaker_notebooks: list[SageMakerNotebook] = field(default_factory=list)
    sagemaker_jobs: list[SageMakerJob] = field(default_factory=list)
    sagemaker_feature_groups: list[SageMakerFeatureGroup] = field(default_factory=list)
    sagemaker_pipelines: list[SageMakerPipeline] = field(default_factory=list)
    sagemaker_monitoring_schedules: list[SageMakerMonitoringSchedule] = field(
        default_factory=list
    )
    sagemaker_inference_recommendations: list[
        SageMakerInferenceRecommendation
    ] = field(default_factory=list)
    sagemaker_cost_coverage: SageMakerCostCoverage | None = None
    sagemaker_savings_plans: SageMakerSavingsPlanCoverage | None = None
    redshift_clusters: list[RedshiftCluster] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)
    actor_events: list[ActorEvent] = field(default_factory=list)
    producer_candidates: list[ProducerCandidate] = field(default_factory=list)
    previous_results: list[PreviousResult] = field(default_factory=list)
    currency: str = "USD"
    athena_coverage: AthenaCoverage | None = None
    athena_actor_usage: list[AthenaActorUsage] = field(default_factory=list)
    glue_cost_coverage: GlueCostCoverage | None = None
    redshift_cost_coverage: RedshiftCostCoverage | None = None
    s3_buckets: list[S3Bucket] = field(default_factory=list)
    s3_prefixes: list[S3Prefix] = field(default_factory=list)
    s3_multipart: list[S3MultipartUpload] = field(default_factory=list)
    s3_cost_coverage: S3CostCoverage | None = None
    #: O que está ligado em cada bucket — e, com isso, se dá para saber se um
    #: objeto é lido. Sem uma destas fontes, só se conhece a última escrita.
    s3_bucket_configs: list[S3BucketConfig] = field(default_factory=list)

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
