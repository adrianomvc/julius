"""Confiança (0–1) derivada da cobertura de evidência do detector."""

from __future__ import annotations

from julius.config import Config


def score_and_label(
    observed_runs: int,
    coverage_days: int,
    has_optional_metrics: bool,
    config: Config,
    *,
    from_ai: bool = False,
) -> tuple[float, str]:
    """Regra determinística: runs + cobertura + métricas opcionais.

    - runs/cobertura abaixo do mínimo → Baixa
    - mínimo satisfeito sem métricas opcionais → Média
    - mínimo + métricas opcionais → Alta
    A IA nunca eleva a confiança (parâmetro `from_ai` só reduz).
    """
    th = config.thresholds
    if observed_runs < th.min_runs or coverage_days < th.min_coverage_days:
        value = 0.45
    elif has_optional_metrics:
        value = 0.86
    else:
        value = 0.68

    if from_ai:
        value = min(value, 0.68)

    if value >= 0.80:
        label = "Alta"
    elif value >= 0.55:
        label = "Média"
    else:
        label = "Baixa"
    return round(value, 2), label


def coverage_ratio(observed_runs: int, coverage_days: int, config: Config) -> float:
    """Fração da janela coberta pela evidência (0–1)."""
    th = config.thresholds
    runs_ratio = min(1.0, observed_runs / max(1, th.min_runs))
    days_ratio = min(1.0, coverage_days / max(1, th.min_coverage_days))
    return round(0.5 * runs_ratio + 0.5 * days_ratio, 2)
