"""Cobrança e custo atribuído: fatura por serviço, rateio Glue e processo."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models.window_math import monthly_factor
from julius.collection.settings import ANALYSIS_WINDOW_DAYS


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
        return monthly_factor(self.window_days)

    @property
    def monthly_cost(self) -> float:
        """Custo do processo por mês — realizado na janela, não projetado.

        Substitui a antiga projeção para fim de mês, que multiplicava um MTD
        de poucos dias e virava o teto de economia de todas as oportunidades
        do processo.
        """
        return self.total_cost_window * self.monthly_factor


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
    # Buckets que a alocação de fato rateou a algum ativo coletado. É registro
    # do que aconteceu, não a tabela de classificação: a cobertura sabe o que
    # ela mesma atribuiu, sem consultar conhecimento de domínio.
    allocated_buckets: list[str] = field(default_factory=list)
    unknown_usage_types: list[str] = field(default_factory=list)
    cost_quality: str = "unavailable"
    modeled_ratio: float | None = None
    allocation_version: str = ""
    gaps: list[str] = field(default_factory=list)

    @property
    def unattributed_cost(self) -> float:
        """Cobrança que não foi rateada a nenhum ativo coletado."""
        rateados = set(self.allocated_buckets)
        return round(
            sum(
                value
                for name, value in self.buckets.items()
                if name not in rateados
            ),
            2,
        )


@dataclass
class RedshiftCostCoverage:
    """Cobrança Redshift da janela, separada entre compute e o resto.

    A separação não é organizacional: um cluster pausado para de cobrar compute
    e continua cobrando armazenamento e snapshot. Um achado de ociosidade só
    pode reivindicar o compute — reivindicar a linha inteira superestimaria a
    recomendação justamente onde ela precisa ser confiável.
    """

    period_start: str = ""
    data_through: str = ""
    cost_metric: str = ""
    currency: str = "USD"
    net_cost: float | None = None
    buckets: dict[str, float] = field(default_factory=dict)
    unknown_usage_types: list[str] = field(default_factory=list)
    cost_quality: str = "unavailable"
    allocation_version: str = ""
    gaps: list[str] = field(default_factory=list)

    def compute_cost(self, compute_buckets: frozenset[str] | set[str]) -> float:
        """O que deixa de ser cobrado quando o cluster para."""
        return round(
            sum(
                value
                for name, value in self.buckets.items()
                if name in compute_buckets
            ),
            6,
        )
