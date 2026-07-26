"""Modelos financeiros para Step Functions."""

from __future__ import annotations

from julius.collection.models import StateMachine
from julius.config import Config
from julius.findings.opportunity import Estimation


def standard_to_express_saving(sm: StateMachine, config: Config) -> Estimation:
    """Standard cobra por state transition; Express por request (~25× mais barato)
    para cargas curtas, de alto volume e idempotentes.

    Sem transições contadas no histórico não há baseline: a estimativa sai
    zerada e declara o que falta, em vez de multiplicar por um zero silencioso.
    """
    pricing = config.pricing
    if sm.avg_state_transitions is None:
        return Estimation(
            method="sfn_standard_to_express_v1",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                "histórico de execução não amostrado: transições por execução "
                "desconhecidas",
                "economia não quantificada sem contagem de transições",
            ],
            pricing_region=pricing.region,
            estimation_version=pricing.version,
            saving_quality="unavailable",
        )
    standard = (
        sm.executions_per_month * sm.avg_state_transitions * pricing.sfn_standard_per_transition
    )
    express = sm.executions_per_month * pricing.sfn_express_per_request
    saving = max(0.0, standard - express)
    return Estimation(
        method="sfn_standard_to_express_v1",
        baseline_cost=round(standard, 2),
        projected_cost=round(express, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{sm.executions_per_month} execuções/mês × {sm.avg_state_transitions} "
            f"transições medidas em {sm.sampled_executions} execuções amostradas",
            f"duração média {sm.avg_duration_sec:.0f}s (< limite Express de 5 min)",
            "idempotência ainda não confirmada: Express é at-least-once",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality="measured",
    )


def polling_loop_saving(sm: StateMachine, config: Config) -> Estimation:
    """Loop de polling (Wait→Task→Choice→Wait) gera transições extras evitáveis
    com .sync/callback."""
    pricing = config.pricing
    if sm.poll_extra_transitions is None or sm.avg_state_transitions is None:
        return Estimation(
            method="sfn_polling_loop_v1",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                "loop confirmado na ASL, mas o histórico não foi amostrado",
                "economia não quantificada sem contagem de transições de espera",
            ],
            pricing_region=pricing.region,
            estimation_version=pricing.version,
            saving_quality="unavailable",
        )
    extra = sm.executions_per_month * sm.poll_extra_transitions
    saving = extra * pricing.sfn_standard_per_transition
    return Estimation(
        method="sfn_polling_loop_v1",
        baseline_cost=round(
            sm.executions_per_month
            * sm.avg_state_transitions
            * pricing.sfn_standard_per_transition,
            2,
        ),
        projected_cost=0.0,
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{sm.poll_extra_transitions} transições extras/execução medidas no "
            f"histórico ({sm.sampled_executions} execuções amostradas)",
            f"{sm.executions_per_month} execuções/mês",
            ".sync ou callback (Task Token) elimina o loop de espera",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality="measured",
    )
