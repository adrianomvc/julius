"""Preços e faixas de recuperação — premissas versionadas, não fatos.

As tarifas vivem em `tables/<região>.toml`. Trocar por preço de contrato, ou por
uma consulta datada à Price List API, é substituir um arquivo de dados: nenhuma
regra e nenhum coletor mudam.

Toda tarifa carrega a própria procedência (`Pricing.provenance`), incluindo se
foi conferida contra fonte citável. Enquanto `verified` for falso, o número é
utilizável mas não deve ser apresentado como preço confirmado.
"""

from julius.knowledge.pricing.athena import ATHENA_RECOVERY_RATES, RecoveryBand
from julius.knowledge.pricing.rates import (
    DEFAULT_REGION,
    Pricing,
    UnknownPricingRegionError,
    available_regions,
    load_table,
)
from julius.knowledge.pricing.sagemaker import is_gpu_instance

__all__ = [
    "ATHENA_RECOVERY_RATES",
    "DEFAULT_REGION",
    "Pricing",
    "RecoveryBand",
    "UnknownPricingRegionError",
    "available_regions",
    "is_gpu_instance",
    "load_table",
]
