"""Faixas de recuperação por regra Athena.

    Quanto de um custo baseline uma correção costuma recuperar — conservador,
    esperado e máximo. Faixa zerada significa que a regra não reduz bytes
    cobrados diretamente no modo on-demand."""

from __future__ import annotations

from dataclasses import dataclass


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
