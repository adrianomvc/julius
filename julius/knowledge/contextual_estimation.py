"""Avalia cenários escolhidos pela IA sem confiar à IA o cálculo financeiro."""

from __future__ import annotations

from julius.collection.models import Account
from julius.config import Config
from julius.findings.investigation import AIEstimationProposal, ContextualEstimate
from julius.findings.signal import Signal
from julius.knowledge.rules.stepfunctions.estimation import (
    standard_to_express_saving,
)

_ALLOWED = {
    "GLUE-IS-CAPACITY-REVIEW": "glue_interactive_capacity_reduction_v1",
    "SM-TRAINING-SPOT-CANDIDATE": "sagemaker_managed_spot_training_v1",
    "SFN-STANDARD-TO-EXPRESS": "sfn_standard_to_express_v1",
}


def evaluate_proposal(
    account: Account,
    signal: Signal,
    proposal: AIEstimationProposal,
    config: Config,
) -> ContextualEstimate:
    expected_method = _ALLOWED.get(signal.rule_id)
    if expected_method != proposal.method:
        raise ValueError(
            f"método {proposal.method!r} não é permitido para {signal.rule_id}"
        )
    if proposal.method == "glue_interactive_capacity_reduction_v1":
        return _glue(account, signal, proposal, config)
    if proposal.method == "sagemaker_managed_spot_training_v1":
        return _spot(account, signal)
    return _express(account, signal, config)


def _glue(
    account: Account,
    signal: Signal,
    proposal: AIEstimationProposal,
    config: Config,
) -> ContextualEstimate:
    session = next(
        (
            item
            for item in account.interactive_sessions
            if item.session_id == signal.asset_name
        ),
        None,
    )
    if session is None:
        raise ValueError("sessão proposta não existe no inventário")
    target = float(proposal.target.get("target_dpu") or 0)
    if target <= 0 or target >= session.dpu:
        raise ValueError("target_dpu deve ser positivo e menor que a capacidade atual")
    missing = []
    if len(session.statement_ids) < 3:
        missing.append("três statements comparáveis")
    # O inventário ainda não contém métricas confiáveis de executor/memória.
    # Sem elas o cenário é guardado, mas nenhum número é fabricado.
    missing.extend(["utilização de executores", "memória e spill da Spark UI"])
    return ContextualEstimate(
        method=proposal.method,
        status="needs_evidence",
        baseline_cost=session.allocated_cost,
        missing_evidence=missing,
        assumptions=[
            f"testar {session.dpu:.1f} para {target:.1f} DPU",
            "aceitar no máximo 10% de aumento de duração",
            f"tarifa de referência {config.pricing.glue_dpu_hour:.4f} USD/DPU-h",
        ],
    )


def _spot(
    account: Account,
    signal: Signal,
) -> ContextualEstimate:
    job = next(
        (
            item
            for item in account.sagemaker_jobs
            if item.name == signal.asset_name and item.kind == "training"
        ),
        None,
    )
    if job is None:
        raise ValueError("training job proposto não existe no inventário")
    baseline = job.allocated_cost or job.modeled_cost
    missing = []
    if not job.checkpoint_configured:
        missing.append("checkpoint configurado")
    if baseline is None or baseline <= 0:
        missing.append("custo observado ou modelado")
    if missing:
        return ContextualEstimate(
            method="sagemaker_managed_spot_training_v1",
            status="needs_evidence",
            baseline_cost=baseline,
            missing_evidence=missing,
        )
    if baseline is None:  # pragma: no cover - protegido pelo gate acima
        raise ValueError("custo do training job indisponível")
    baseline_value = float(baseline)
    return ContextualEstimate(
        method="sagemaker_managed_spot_training_v1",
        status="estimated",
        baseline_cost=round(baseline_value, 2),
        estimated_low=round(baseline_value * 0.50, 2),
        estimated_expected=round(baseline_value * 0.70, 2),
        estimated_high=round(baseline_value * 0.90, 2),
        assumptions=[
            "faixa contextual de 50%/70%/90%; não é preço garantido",
            "SLA tolera interrupção e recuperação por checkpoint",
        ],
    )


def _express(
    account: Account,
    signal: Signal,
    config: Config,
) -> ContextualEstimate:
    machine = next(
        (item for item in account.state_machines if item.name == signal.asset_name),
        None,
    )
    if machine is None:
        raise ValueError("state machine proposta não existe no inventário")
    missing = []
    if machine.avg_state_transitions is None:
        missing.append("transições por execução")
    if machine.express_benchmark_duration_ms is None:
        missing.append("benchmark de duração Express")
    if machine.express_benchmark_memory_mb is None:
        missing.append("benchmark de memória Express")
    if missing:
        return ContextualEstimate(
            method="sfn_standard_to_express_v1",
            status="needs_evidence",
            missing_evidence=missing,
        )
    estimation = standard_to_express_saving(machine, config)
    saving = max(0.0, estimation.estimated_saving)
    return ContextualEstimate(
        method="sfn_standard_to_express_v1",
        status="estimated",
        baseline_cost=round(estimation.baseline_cost, 2),
        estimated_low=round(saving * 0.8, 2),
        estimated_expected=round(saving, 2),
        estimated_high=round(
            min(estimation.baseline_cost, saving * 1.1), 2
        ),
        assumptions=list(estimation.assumptions),
    )
