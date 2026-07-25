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
    SAGEMAKER_HOURLY,
    SAGEMAKER_HOURLY_DEFAULT,
    Pricing,
    RecoveryBand,
    is_gpu_instance,
    sagemaker_hourly,
)
from julius.knowledge.thresholds import Thresholds

# Versões usadas nas oportunidades (auditoria/calibração).
JULIUS_VERSION = "0.6.0"
KNOWLEDGE_VERSION = "aws-glue-guidance-2026-07"
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
class Config:
    pricing: Pricing = field(default_factory=Pricing)
    thresholds: Thresholds = field(default_factory=Thresholds)
    weights: Weights = field(default_factory=Weights)
    glue_cost: GlueCostTaxonomy = field(default_factory=GlueCostTaxonomy)
    # Fator de realização padrão (parte da economia efetivamente capturada).
    realization_factor: float = 0.8
    preferred_glue_version: str = "5.1"
    # Mês/ano de referência para "realizável no ano" (dez do ano corrente).
    year_end_month: int = 12


DEFAULT_CONFIG = Config()

# Offset (em meses) até a data provável de implementação, por dificuldade.
IMPL_OFFSET_BY_DIFFICULTY: dict[int, int] = {1: 0, 2: 0, 3: 1, 4: 2, 5: 3}

__all__ = [
    "ALLOCATED_GLUE_BUCKETS",
    "ANALYSIS_WINDOW_DAYS",
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
    "MIN_DAYS_FOR_FORECAST",
    "SAGEMAKER_HOURLY",
    "SAGEMAKER_HOURLY_DEFAULT",
    "UNATTRIBUTED_GLUE_BUCKETS",
    "Config",
    "Pricing",
    "RecoveryBand",
    "Thresholds",
    "Weights",
    "is_gpu_instance",
    "sagemaker_hourly",
]
