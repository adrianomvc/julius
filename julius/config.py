"""Configuração determinística do Julius.

Preços e limiares ficam aqui, versionados, para que toda estimativa seja
reproduzível e auditável. Valores financeiros são mantidos em USD, a mesma
moeda retornada pelo Cost Explorer. Preços são *premissas* explícitas —
troque pelos preços da sua região/contrato ou por uma tabela versionada.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Parâmetros de *como a coleta mede* moram junto dela. Reexportados aqui para
# quem já importava deste módulo antes da separação.
from julius.collection.settings import (
    ANALYSIS_WINDOW_DAYS,
    DATASET_SCHEMA_VERSION,
    DAYS_PER_MONTH,
    DPU_PER_WORKER,
    MIN_DAYS_FOR_FORECAST,
)

__all__ = [
    "ANALYSIS_WINDOW_DAYS",
    "ATHENA_RECOVERY_RATES",
    "DATASET_SCHEMA_VERSION",
    "DAYS_PER_MONTH",
    "DEFAULT_CONFIG",
    "DPU_PER_WORKER",
    "MIN_DAYS_FOR_FORECAST",
    "Config",
    "Pricing",
    "Thresholds",
]

# Preço aproximado (USD/hora) por tipo de instância SageMaker.
SAGEMAKER_HOURLY: dict[str, float] = {
    "ml.t3.medium": 0.05,
    "ml.t3.large": 0.10,
    "ml.m5.large": 0.12,
    "ml.m5.xlarge": 0.23,
    "ml.m5.2xlarge": 0.46,
    "ml.c5.xlarge": 0.21,
    "ml.g4dn.xlarge": 0.53,   # GPU
    "ml.g5.xlarge": 1.00,     # GPU
    "ml.p3.2xlarge": 4.00,    # GPU
}
SAGEMAKER_HOURLY_DEFAULT = 0.18


def sagemaker_hourly(instance_type: str) -> float:
    return SAGEMAKER_HOURLY.get(instance_type, SAGEMAKER_HOURLY_DEFAULT)


def is_gpu_instance(instance_type: str) -> bool:
    return any(f".{fam}" in instance_type for fam in ("g4dn", "g5", "p3", "p4", "p2", "g6"))

# Versões usadas nas oportunidades (auditoria/calibração).
JULIUS_VERSION = "0.6.0"
KNOWLEDGE_VERSION = "aws-glue-guidance-2026-07"
ATHENA_RECOVERY_VERSION = "athena-recovery-1.0"
GLUE_COST_VERSION = "glue-cost-allocation-1.0"


@dataclass(frozen=True)
class RecoveryBand:
    """Faixa conservadora/esperada/máxima aplicada ao custo baseline."""

    low: float
    expected: float
    high: float


ATHENA_RECOVERY_RATES: dict[str, RecoveryBand] = {
    "select_star": RecoveryBand(0.15, 0.30, 0.50),
    "full_scan": RecoveryBand(0.20, 0.40, 0.60),
    "missing_partition_filter": RecoveryBand(0.30, 0.50, 0.70),
    "table_not_partitioned": RecoveryBand(0.20, 0.40, 0.60),
    "row_format_compression": RecoveryBand(0.20, 0.40, 0.60),
    "columnar_compression": RecoveryBand(0.10, 0.25, 0.40),
    "legacy_result_reuse": RecoveryBand(0.10, 0.20, 0.30),
    # Não reduzem diretamente bytes cobrados no modo on-demand.
    "partition_projection": RecoveryBand(0.0, 0.0, 0.0),
    "small_files": RecoveryBand(0.0, 0.0, 0.0),
    "recurrent_failures": RecoveryBand(0.0, 0.0, 0.0),
}

# Classificação versionada de `USAGE_TYPE` do Cost Explorer em buckets de
# cobrança Glue. A ordem importa: o primeiro marcador encontrado no usage type
# normalizado (minúsculo, sem prefixo de região) define o bucket.
GLUE_USAGE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("databrew", "databrew"),
    ("dataquality", "data_quality"),
    ("data-quality", "data_quality"),
    ("crawler", "crawler"),
    ("session", "interactive_session"),
    ("notebook", "interactive_session"),
    ("flex", "flex"),
    ("objectstorage", "catalog"),
    ("request", "catalog"),
    ("catalog", "catalog"),
    ("dpu-hour", "etl_job"),
    ("dpu_hour", "etl_job"),
    ("etl", "etl_job"),
)

# Buckets rateados a ativos coletados e buckets que permanecem sem atribuição.
ALLOCATED_GLUE_BUCKETS: frozenset[str] = frozenset(
    {"etl_job", "flex", "crawler", "interactive_session", "databrew"}
)
UNATTRIBUTED_GLUE_BUCKETS: frozenset[str] = frozenset(
    {"catalog", "data_quality", "other"}
)


@dataclass(frozen=True)
class Pricing:
    """Preços (premissas) por região. Ajustáveis via config."""

    region: str = "sa-east-1"
    currency: str = "USD"
    version: str = "2.0-usd"
    # USD/DPU-hora. Fallback versionado; a cobrança permanece no Cost Explorer.
    glue_dpu_hour: float = 0.44
    glue_flex_dpu_hour: float = 0.29
    glue_ray_mdpu_hour: float = 0.44
    # DataBrew usa node-hour, não Glue DPU-hour.
    databrew_node_hour: float = 0.48
    # USD/TB escaneado no Athena (fallback versionado; a cobrança real vem do
    # Cost Explorer quando a coleta é reconciliada).
    athena_per_tb_usd: float = 5.0
    # Step Functions: USD/state transition (Standard) e USD/request (Express).
    sfn_standard_per_transition: float = 0.000025
    sfn_express_per_request: float = 0.000001

    def glue_rate(self, execution_class: str = "STANDARD") -> float:
        return (
            self.glue_flex_dpu_hour
            if execution_class.upper() == "FLEX"
            else self.glue_dpu_hour
        )


@dataclass(frozen=True)
class Thresholds:
    """Limiares determinísticos compartilhados pelos detectores ativos."""

    # Alvo de utilização de CPU para dimensionamento (acima disso, saudável).
    utilization_target: float = 0.75
    # CPU média abaixo disso sinaliza superdimensionamento.
    low_cpu: float = 0.45
    # Idle timeout (min) considerado alto para sessões interativas.
    session_idle_timeout_high_min: int = 60
    # DPU mínima útil de uma interactive session.
    session_min_dpu: int = 2
    # Bytes escaneados/execução no Athena considerado "excessivo" (256 GB).
    athena_high_scan_bytes: int = 256 * 1024**3
    # Nº mínimo de execuções observadas para confiança não-baixa.
    min_runs: int = 5
    # Cobertura mínima (dias) da janela para confiança não-baixa.
    min_coverage_days: int = 14
    # Taxa de falha (incl. retries) acima da qual o desperdício de DPU é sinalizado.
    high_failure_rate: float = 0.10
    # Base é considerada "sem uso" com até este nº de toques na janela.
    unused_touches_max: int = 0
    # Base "pouco usada": toques acima de unused_touches_max e até este limite.
    low_touches_max: int = 30
    # Nº mínimo de execuções/mês para o job escritor ser considerado recorrente.
    recurring_runs_min: int = 4
    # Timeout é "excessivo" se > ratio × duração média (e acima do mínimo absoluto).
    timeout_excess_ratio: float = 4.0
    timeout_excess_min_minutes: int = 120
    # Worker type é "grande demais" quando a CPU fica abaixo disto (types G.4X/G.8X).
    worker_type_low_cpu: float = 0.30
    # Evidência complementar obrigatória antes de reduzir capacidade.
    worker_memory_pressure_high: float = 0.75
    worker_disk_pressure_high: float = 0.70
    worker_utilization_low: float = 0.45
    executor_gap_high: float = 0.35
    task_skew_high: float = 1.0
    task_duration_cv_high: float = 0.75
    high_job_frequency_monthly: int = 100
    # Estimativas conservadoras: parte do desperdício medido que esperamos capturar.
    conservative_realization: float = 0.70
    # Alocação de custo Glue: fração máxima de DPU-hora apenas estimada (sem
    # DPUSeconds reportado) tolerada para considerar a alocação reconciliada.
    glue_estimated_dpu_tolerance: float = 0.05
    # Banda aceita entre o consumo modelado e a cobrança do bucket no Cost
    # Explorer (mesma disciplina da reconciliação Athena/CloudWatch).
    glue_reconciliation_low: float = 0.95
    glue_reconciliation_high: float = 1.05
    # Step Functions: volume/duração para avaliar Standard → Express.
    sfn_express_min_executions: int = 5000
    sfn_short_duration_sec: int = 300
    # SageMaker: idle shutdown "alto" (min) e horas ociosas/dia relevantes.
    sm_idle_shutdown_high_min: int = 120
    sm_idle_hours_min: float = 1.0
    # SageMaker: endpoint "sem uso" com até este nº de invocações/mês.
    sm_endpoint_unused_invocations: int = 50


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
class Config:
    pricing: Pricing = field(default_factory=Pricing)
    thresholds: Thresholds = field(default_factory=Thresholds)
    weights: Weights = field(default_factory=Weights)
    # Fator de realização padrão (parte da economia efetivamente capturada).
    realization_factor: float = 0.8
    preferred_glue_version: str = "5.1"
    # Mês/ano de referência para "realizável no ano" (dez do ano corrente).
    year_end_month: int = 12


DEFAULT_CONFIG = Config()

# Offset (em meses) até a data provável de implementação, por dificuldade.
IMPL_OFFSET_BY_DIFFICULTY: dict[int, int] = {1: 0, 2: 0, 3: 1, 4: 2, 5: 3}
