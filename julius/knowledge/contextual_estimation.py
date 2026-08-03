"""Avalia cenários escolhidos pela IA sem confiar à IA o cálculo financeiro.

A divisão é a mesma em todos os métodos: a análise contextual escolhe o cenário
— qual capacidade testar, qual instância comparar, qual integração trocar — e o
motor executa a fórmula sobre o inventário. A IA nunca devolve um número; ela
devolve qual conta fazer, e a conta é feita aqui, com a tarifa da região e o
custo já atribuído ao ativo.

O mapa `_ALLOWED` é o que impede a proposta de virar carta branca: um método só
pode ser proposto para o sinal que ele responde. Sinal de shuffle não pede
cálculo de spot, e um `rule_id` sem entrada aqui não aceita proposta nenhuma.
"""

from __future__ import annotations

from dataclasses import replace

from julius.collection.models import Account
from julius.config import Config
from julius.findings.investigation import AIEstimationProposal, ContextualEstimate
from julius.findings.maturity import Maturity
from julius.findings.signal import Signal
from julius.knowledge.estimate_contract import enforce
from julius.knowledge.rules.glue.estimation import window_baseline
from julius.knowledge.rules.sagemaker.estimation import gpu_to_cpu_saving
from julius.knowledge.rules.stepfunctions.estimation import (
    standard_to_express_saving,
)

_ALLOWED = {
    "GLUE-IS-CAPACITY-REVIEW": "glue_interactive_capacity_reduction_v1",
    "SM-TRAINING-SPOT-CANDIDATE": "sagemaker_managed_spot_training_v1",
    # O sinal de código diz **por que** o spot está bloqueado; o cenário a
    # avaliar é o mesmo, e `_spot` já recusa sem checkpoint configurado.
    "SM-CODE-NO-CHECKPOINT": "sagemaker_managed_spot_training_v1",
    "SM-CODE-CPU-ONLY-ON-GPU": "sagemaker_gpu_to_cpu_instance_v1",
    "SFN-STANDARD-TO-EXPRESS": "sfn_standard_to_express_v1",
    "GLUE-CODE-SHUFFLE": "glue_shuffle_reduction_v1",
}

#: O parâmetro que cada método espera em `AIEstimationProposal.target`, e o
#: limite que a validação abaixo cobra. Só dois métodos pedem alvo; para os
#: outros o cenário está inteiro no inventário e `target` vai vazio.
#:
#: Mora aqui, colado na validação que o aplica, porque quem gera o briefing da
#: análise contextual precisa anunciá-lo sem reescrever a regra à mão. Foi
#: exatamente a cópia à mão que deixou dois métodos deste mapa fora do briefing
#: — implementados, testados e impossíveis de propor.
_TARGET = {
    "glue_shuffle_reduction_v1": (
        "expected_reduction",
        "fração da duração a reduzir, maior que 0 e no máximo 0.5",
    ),
    "glue_interactive_capacity_reduction_v1": (
        "target_dpu",
        "capacidade a testar, positiva e menor que a atual",
    ),
}


def allowed_methods() -> dict[str, str]:
    """Qual método cada `rule_id` aceita — a fonte do que o briefing anuncia.

    Devolve cópia: o mapa é o que impede a proposta de virar carta branca, e
    quem lê para montar texto não pode alterá-lo.
    """
    return dict(_ALLOWED)


def target_parameter(method: str) -> tuple[str, str] | None:
    """A chave obrigatória em `target`, e o limite dela. `None` quando não há."""
    return _TARGET.get(method)


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
        bruta = _glue(account, signal, proposal, config)
    elif proposal.method == "sagemaker_managed_spot_training_v1":
        bruta = _spot(account, signal)
    elif proposal.method == "sagemaker_gpu_to_cpu_instance_v1":
        bruta = _gpu_to_cpu(account, signal, config)
    elif proposal.method == "glue_shuffle_reduction_v1":
        bruta = _shuffle(account, signal, proposal, config)
    else:
        bruta = _express(account, signal, config)
    return enforce(_com_procedencia(bruta, signal, config), baseline_currency=config.pricing.currency)


def _com_procedencia(
    estimate: ContextualEstimate, signal: Signal, config: Config
) -> ContextualEstimate:
    """Carimba de onde veio a tarifa e sobre qual evidência a conta foi feita.

    Fica num lugar só porque é idêntico para os cinco métodos, e porque a
    alternativa — cada método preenchendo os seus — é como um deles acaba sem
    região e ninguém percebe até dois números de continentes diferentes serem
    somados.
    """
    return replace(
        estimate,
        pricing_region=config.pricing.region,
        currency=config.pricing.currency,
        period="monthly",
        method_version=estimate.method.rsplit("_", 1)[-1],
        evidence_hash=signal.evidence_signature(),
        maturity=(
            Maturity.PILOT_REQUIRED
            if estimate.status == "estimated"
            else Maturity.CONTEXTUAL_ESTIMATE
        ),
    )


def _gpu_to_cpu(
    account: Account,
    signal: Signal,
    config: Config,
) -> ContextualEstimate:
    """A troca de instância, calculada pela mesma função da regra determinística.

    A regra já sabe fazer esta conta — ela só não a faz quando falta telemetria
    que prove que a GPU ficou parada. O que a análise contextual acrescenta é
    exatamente essa leitura, feita sobre o script inteiro em vez de sobre a AST.
    """
    job = next(
        (item for item in account.sagemaker_jobs if item.name == signal.asset_name),
        None,
    )
    if job is None:
        raise ValueError("job proposto não existe no inventário")
    estimation = gpu_to_cpu_saving(job, config)
    if estimation.saving_quality == "unavailable":
        return ContextualEstimate(
            method="sagemaker_gpu_to_cpu_instance_v1",
            status="needs_evidence",
            missing_evidence=list(estimation.assumptions),
        )
    return ContextualEstimate(
        method="sagemaker_gpu_to_cpu_instance_v1",
        status="estimated",
        baseline_cost=estimation.baseline_cost,
        # A faixa cobre a hipótese que a conta não fecha: sem GPU em uso a
        # duração tende a não mudar, mas pode aumentar numa instância menor.
        estimated_low=round(estimation.estimated_saving * 0.50, 2),
        estimated_expected=round(estimation.estimated_saving * 0.80, 2),
        estimated_high=round(estimation.estimated_saving, 2),
        assumptions=list(estimation.assumptions),
    )


def _shuffle(
    account: Account,
    signal: Signal,
    proposal: AIEstimationProposal,
    config: Config,
) -> ContextualEstimate:
    """Redução de shuffle, e por que ela nunca sai daqui como número seco.

    O sinal existe porque `join`/`groupBy` são fato e a economia não: a mesma
    transformação é indispensável num job e desperdício em outro. O motor
    calcula a faixa sobre o custo real do job, e só quando há medição de shuffle
    para ancorá-la — sem ela, o que sobraria seria a fração fixa que este
    caminho foi criado para eliminar.
    """
    job = next(
        (item for item in account.glue_jobs if item.name == signal.asset_name), None
    )
    if job is None:
        raise ValueError("job proposto não existe no inventário")
    fracao = float(proposal.target.get("expected_reduction") or 0)
    if not 0 < fracao <= 0.5:
        raise ValueError("expected_reduction deve estar entre 0 e 0.5")
    # A mesma base das regras determinísticas de Glue: DPU-hora da janela pela
    # tarifa que a alocação da fatura escolher.
    baseline_value, _, _ = window_baseline(job, config.pricing)
    missing = []
    if not (job.shuffle_write_bytes or job.shuffle_read_bytes or job.has_spill_evidence):
        missing.append("shuffle read/write ou spill medidos no Spark event log")
    if baseline_value <= 0:
        missing.append("DPU-hora observada do job na janela")
    if missing:
        return ContextualEstimate(
            method="glue_shuffle_reduction_v1",
            status="needs_evidence",
            baseline_cost=round(baseline_value, 2) or None,
            missing_evidence=missing,
        )
    return ContextualEstimate(
        method="glue_shuffle_reduction_v1",
        status="estimated",
        baseline_cost=round(baseline_value, 2),
        estimated_low=round(baseline_value * fracao * 0.4, 2),
        estimated_expected=round(baseline_value * fracao * 0.7, 2),
        estimated_high=round(baseline_value * fracao, 2),
        assumptions=[
            f"redução de {fracao:.0%} da duração proposta pela análise contextual",
            "faixa condicionada a benchmark A/B com o mesmo volume de entrada",
            "chaves e estratégia de join revisadas sem alterar o resultado",
        ],
    )


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
