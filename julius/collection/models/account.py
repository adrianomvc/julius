"""A conta inteira, com tudo que foi coletado."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models.assets import (
    ActorEvent,
    PreviousResult,
    ProducerCandidate,
    RedshiftCluster,
    SageMakerApp,
    SageMakerEndpoint,
    Schedule,
    StateMachine,
    Table,
)
from julius.collection.models.athena import (
    AthenaActorUsage,
    AthenaCoverage,
    AthenaQuery,
)
from julius.collection.models.cost import (
    GlueCostCoverage,
    ProcessCost,
    ServiceCost,
)
from julius.collection.models.glue import (
    DataBrewJob,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    InteractiveSession,
)
from julius.collection.models.health import CollectionHealth
from julius.collection.settings import ANALYSIS_WINDOW_DAYS


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
