"""Modelos financeiros para Step Functions."""

from __future__ import annotations

from julius.collection.models import StateMachine
from julius.config import Config
from julius.findings.opportunity import Estimation


def standard_to_express_saving(sm: StateMachine, config: Config) -> Estimation:
    """Compara transições Standard com requests + GB-s medidos no Express.

    Sem transições contadas no histórico não há baseline: a estimativa sai
    zerada e declara o que falta, em vez de multiplicar por um zero silencioso.
    """
    pricing = config.pricing
    if (
        sm.avg_state_transitions is None
        or sm.express_benchmark_duration_ms is None
        or sm.express_benchmark_memory_mb is None
    ):
        return Estimation(
            method="sfn_standard_to_express_v2",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                "contrafactual Express incompleto",
                "economia não quantificada sem transições, duração e memória de benchmark",
            ],
            pricing_region=pricing.region,
            estimation_version=pricing.version,
            saving_quality="unavailable",
        )
    standard = (
        sm.executions_per_month * sm.avg_state_transitions * pricing.sfn_standard_per_transition
    )
    # Express arredonda duração para blocos de 100 ms e memória para 64 MB.
    billed_ms = max(100, ((sm.express_benchmark_duration_ms + 99) // 100) * 100)
    billed_mb = max(64, ((sm.express_benchmark_memory_mb + 63) // 64) * 64)
    gb_seconds = sm.executions_per_month * (billed_ms / 1000) * (billed_mb / 1024)
    express = (
        sm.executions_per_month * pricing.sfn_express_per_request
        + gb_seconds * pricing.sfn_express_per_gb_second
    )
    saving = max(0.0, standard - express)
    return Estimation(
        method="sfn_standard_to_express_v2",
        baseline_cost=round(standard, 2),
        projected_cost=round(express, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{sm.executions_per_month} execuções/mês × {sm.avg_state_transitions} "
            f"transições medidas em {sm.sampled_executions} execuções amostradas",
            f"benchmark Express faturado em {billed_ms} ms e {billed_mb} MB",
            f"{gb_seconds:.3f} GB-s/mês mais requests",
            "idempotência confirmada separadamente: Express é at-least-once",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        # Transições contadas no histórico, não supostas — mas o baseline
        # segue sendo tarifa versionada sobre consumo medido, que é o que
        # `modeled` significa na escala. `measured` ali descrevia a coleta,
        # não a origem do número, e caía no fallback da pior nota.
        baseline_quality="modeled",
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
        # Transições contadas no histórico, não supostas — mas o baseline
        # segue sendo tarifa versionada sobre consumo medido, que é o que
        # `modeled` significa na escala. `measured` ali descrevia a coleta,
        # não a origem do número, e caía no fallback da pior nota.
        baseline_quality="modeled",
    )
