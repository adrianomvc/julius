"""Composição da configuração determinística do Julius.

Os valores não moram mais aqui. Preço e faixa de recuperação estão em
`knowledge/pricing`, limiares em `knowledge/thresholds`, e os parâmetros de
*como a coleta mede* em `collection/settings`. Este módulo só junta as três
coisas num objeto e guarda as versões que carimbam cada oportunidade.

É o ponto de composição: importa de baixo, e ninguém de baixo importa daqui.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.settings import (
    ANALYSIS_WINDOW_DAYS,
    BOOTSTRAP_WINDOW_DAYS,
    DATASET_SCHEMA_VERSION,
    DAYS_PER_MONTH,
    DPU_PER_WORKER,
    MIN_DAYS_FOR_FORECAST,
)
from julius.knowledge.glue_cost import (
    ALLOCATED_GLUE_BUCKETS,
    GLUE_USAGE_TYPE_MARKERS,
    UNATTRIBUTED_GLUE_BUCKETS,
)
from julius.knowledge.pricing import (
    ATHENA_RECOVERY_RATES,
    Pricing,
    RecoveryBand,
    is_gpu_instance,
)
from julius.knowledge.redshift_cost import (
    REDSHIFT_COMPUTE_BUCKETS,
    REDSHIFT_USAGE_TYPE_MARKERS,
    UNATTRIBUTED_REDSHIFT_BUCKETS,
)
from julius.knowledge.s3_cost import (
    S3_STORAGE_BUCKETS,
    S3_USAGE_TYPE_MARKERS,
    UNATTRIBUTED_S3_BUCKETS,
)
from julius.knowledge.sagemaker_cost import (
    ALLOCATABLE_SAGEMAKER_BUCKETS,
    SAGEMAKER_USAGE_TYPE_MARKERS,
    UNATTRIBUTED_SAGEMAKER_BUCKETS,
)
from julius.knowledge.thresholds import Thresholds

# Versões usadas nas oportunidades (auditoria/calibração).
JULIUS_VERSION = "0.6.0"
KNOWLEDGE_VERSION = "aws-glue-guidance-2026-07"
KNOWLEDGE_VERSIONS = {
    "glue": "glue-knowledge-2026-07",
    "athena": "athena-knowledge-2026-07",
    "stepfunctions": "stepfunctions-knowledge-2026-07",
    "sagemaker": "sagemaker-knowledge-2026-07",
    "redshift": "redshift-knowledge-2026-07",
    "s3": "s3-knowledge-2026-07",
}
ATHENA_RECOVERY_VERSION = "athena-recovery-1.0"
GLUE_COST_VERSION = "glue-cost-allocation-1.0"


@dataclass(frozen=True)
class Weights:
    """Perfil de pesos do score de ganho (balanced por padrão)."""

    profile: str = "balanced"
    economia: float = 0.35
    desempenho_confiabilidade: float = 0.20
    governanca_risco: float = 0.25
    alcance: float = 0.10
    tendencia: float = 0.10


@dataclass(frozen=True)
class GlueCostTaxonomy:
    """A tradução de `USAGE_TYPE` em buckets, composta para uma execução.

    A coleta precisa dela mas não pode importá-la — a seta aponta para baixo.
    Chega pelo `Config`, que é montado aqui, acima das duas camadas.
    """

    usage_type_markers: tuple[tuple[str, str], ...] = GLUE_USAGE_TYPE_MARKERS
    allocatable_buckets: frozenset[str] = ALLOCATED_GLUE_BUCKETS
    unattributed_buckets: frozenset[str] = UNATTRIBUTED_GLUE_BUCKETS
    version: str = GLUE_COST_VERSION


@dataclass(frozen=True)
class RedshiftCostTaxonomy:
    """A mesma tradução, para um serviço com uma distinção própria.

    Compute e armazenamento não se comportam igual: pausar o cluster para um e
    não o outro. A taxonomia separa os dois porque a recomendação depende disso.
    """

    usage_type_markers: tuple[tuple[str, str], ...] = REDSHIFT_USAGE_TYPE_MARKERS
    compute_buckets: frozenset[str] = REDSHIFT_COMPUTE_BUCKETS
    unattributed_buckets: frozenset[str] = UNATTRIBUTED_REDSHIFT_BUCKETS
    version: str = "1.0"


@dataclass(frozen=True)
class SageMakerCostTaxonomy:
    """Classificação de `USAGE_TYPE` para rateio por componente SageMaker."""

    usage_type_markers: tuple[tuple[str, str], ...] = SAGEMAKER_USAGE_TYPE_MARKERS
    allocatable_buckets: frozenset[str] = ALLOCATABLE_SAGEMAKER_BUCKETS
    unattributed_buckets: frozenset[str] = UNATTRIBUTED_SAGEMAKER_BUCKETS
    version: str = "1.0"


@dataclass(frozen=True)
class S3CostTaxonomy:
    """A mesma tradução, para o serviço onde a distinção muda a recomendação.

    Armazenamento e request não caem juntos: apagar objeto reduz armazenamento e
    não toca em request; compactar arquivo pequeno reduz request e quase não move
    armazenamento. Só `storage_buckets` pode ser reivindicado por uma
    recomendação de exclusão, e é por isso que ele é um campo e não um detalhe.
    """

    usage_type_markers: tuple[tuple[str, str], ...] = S3_USAGE_TYPE_MARKERS
    storage_buckets: frozenset[str] = S3_STORAGE_BUCKETS
    unattributed_buckets: frozenset[str] = UNATTRIBUTED_S3_BUCKETS
    version: str = "1.0"


@dataclass(frozen=True)
class Config:
    pricing: Pricing = field(default_factory=Pricing.for_region)
    thresholds: Thresholds = field(default_factory=Thresholds)
    weights: Weights = field(default_factory=Weights)
    glue_cost: GlueCostTaxonomy = field(default_factory=GlueCostTaxonomy)
    redshift_cost: RedshiftCostTaxonomy = field(default_factory=RedshiftCostTaxonomy)
    sagemaker_cost: SageMakerCostTaxonomy = field(default_factory=SageMakerCostTaxonomy)
    s3_cost: S3CostTaxonomy = field(default_factory=S3CostTaxonomy)
    # Fator de realização padrão (parte da economia efetivamente capturada).
    realization_factor: float = 0.8
    # O mesmo, para economia que nasceu de interpretação e passou por piloto.
    #
    # Mais duro que o determinístico de propósito, e a razão não é o tamanho do
    # número — é a origem dele. Uma oportunidade determinística parte de fato
    # medido e o piloto confirma a conta; uma estimativa contextual parte de
    # leitura de código, e o piloto confirma **uma execução**. O que se generaliza
    # dali é menos, e o fator diz isso em vez de fingir que as duas coisas têm a
    # mesma procedência.
    #
    # Decisão do dono do produto, 2026-08-03.
    contextual_realization_factor: float = 0.6
    preferred_glue_version: str = "5.1"
    # Mês/ano de referência para "realizável no ano" (dez do ano corrente).
    year_end_month: int = 12


DEFAULT_CONFIG = Config()

# Offset (em meses) até a data provável de implementação, por dificuldade.
IMPL_OFFSET_BY_DIFFICULTY: dict[int, int] = {1: 0, 2: 0, 3: 1, 4: 2, 5: 3}

__all__ = [
    "ALLOCATED_GLUE_BUCKETS",
    "ANALYSIS_WINDOW_DAYS",
    "BOOTSTRAP_WINDOW_DAYS",
    "ATHENA_RECOVERY_RATES",
    "ATHENA_RECOVERY_VERSION",
    "DATASET_SCHEMA_VERSION",
    "DAYS_PER_MONTH",
    "DEFAULT_CONFIG",
    "DPU_PER_WORKER",
    "GLUE_COST_VERSION",
    "GLUE_USAGE_TYPE_MARKERS",
    "IMPL_OFFSET_BY_DIFFICULTY",
    "JULIUS_VERSION",
    "KNOWLEDGE_VERSION",
    "KNOWLEDGE_VERSIONS",
    "MIN_DAYS_FOR_FORECAST",
    "UNATTRIBUTED_GLUE_BUCKETS",
    "Config",
    "Pricing",
    "RecoveryBand",
    "SageMakerCostTaxonomy",
    "Thresholds",
    "Weights",
    "is_gpu_instance",
]
