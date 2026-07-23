"""Modelos financeiros para Step Functions."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import StateMachine
from julius.opportunities.base import Estimation


def standard_to_express_saving(sm: StateMachine, config: Config) -> Estimation:
    """Standard cobra por state transition; Express por request (~25× mais barato)
    para cargas curtas, de alto volume e idempotentes."""
    pricing = config.pricing
    standard = sm.executions_per_month * sm.avg_state_transitions * pricing.sfn_standard_per_transition
    express = sm.executions_per_month * pricing.sfn_express_per_request
    saving = max(0.0, standard - express)
    return Estimation(
        method="sfn_standard_to_express_v1",
        baseline_cost=round(standard, 2),
        projected_cost=round(express, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{sm.executions_per_month} execuções/mês × {sm.avg_state_transitions} transições",
            f"duração média {sm.avg_duration_sec:.0f}s (< limite Express de 5 min)",
            "carga idempotente que tolera semântica at-least-once do Express",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def polling_loop_saving(sm: StateMachine, config: Config) -> Estimation:
    """Loop de polling (Wait→Task→Choice→Wait) gera transições extras evitáveis
    com .sync/callback."""
    pricing = config.pricing
    extra = sm.executions_per_month * sm.poll_extra_transitions
    saving = extra * pricing.sfn_standard_per_transition
    return Estimation(
        method="sfn_polling_loop_v1",
        baseline_cost=round(sm.executions_per_month * sm.avg_state_transitions * pricing.sfn_standard_per_transition, 2),
        projected_cost=0.0,
        estimated_saving=round(saving, 2),
        assumptions=[
            f"~{sm.poll_extra_transitions} transições extras/execução por polling",
            f"{sm.executions_per_month} execuções/mês",
            ".sync ou callback (Task Token) elimina o loop de espera",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
