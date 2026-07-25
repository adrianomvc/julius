"""Modelo financeiro para bases sem uso geradas por processo recorrente."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import GlueJob, Table
from julius.opportunities.base import Estimation


def unused_output_saving(table: Table, writer: GlueJob, config: Config) -> Estimation:
    """Base sem toques produzida por job recorrente → todo o compute é desperdício.

    Ganho conservador = custo mensal do job escritor (recuperável ao pausar/
    descomissionar o processo). Assume que a tabela é o destino principal do job.
    """
    pricing = config.pricing
    baseline = writer.window_dpu_hours * pricing.glue_dpu_hour
    return Estimation(
        method="data_unused_output_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=0.0,
        estimated_saving=round(baseline, 2),
        assumptions=[
            f"tabela sem toques na janela ({table.touches_90d} acessos)",
            f"job escritor '{writer.name}' roda {writer.runs_per_month}×/mês",
            "tabela é o destino principal do job (confirmar antes de descomissionar)",
            "não considera o storage S3 ocupado (ganho adicional)",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def low_use_saving(table: Table, writer: GlueJob, config: Config, recover: float = 0.5) -> Estimation:
    """Base pouco tocada por um único consumidor → parte do compute pode ser
    recuperada (descomissionar/consolidar) ou o produto formalizado. Conservador:
    metade do custo do job (a decisão é de negócio)."""
    pricing = config.pricing
    baseline = writer.window_dpu_hours * pricing.glue_dpu_hour
    saving = baseline * recover
    return Estimation(
        method="data_low_use_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{table.touches_90d} toques em 90 dias por {table.consuming_communities} comunidade(s)",
            f"job escritor '{writer.name}' roda {writer.runs_per_month}×/mês",
            f"~{int(recover * 100)}% recuperável (descomissionar/consolidar) — decisão de negócio",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
