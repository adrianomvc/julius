"""O único caminho em que a IA devolve número — e tudo que o cerca.

Existe desperdício comprovado para o qual nenhuma fórmula do motor fecha. Uma UDF
Python impede otimização do Spark: isso é fato do script. Quanto ela custa neste
job depende da cardinalidade, do plano, do que a UDF faz e de quantas vezes ela é
chamada por linha — e nenhuma métrica coletável responde. Hoje o motor devolve
`needs_evidence` e a conversa acaba: o sinal fica sem ordem de grandeza, ao lado
de outros catorze, e o que decide qual investigar primeiro passa a ser nada.

Este módulo é a resposta, e ela é estreita de propósito.

**A IA não escolhe o baseline.** Ela recebe o sinal e devolve a faixa; o custo do
ativo é resolvido aqui, pelo mesmo caminho que as regras determinísticas usam. Um
baseline proposto pela análise seria a forma mais direta de um número inventado
ganhar aparência de conta feita.

**A elegibilidade é por `rule_id`, e a lista nasce curta.** `_ELEGIVEIS` não é
uma categoria — é uma lista de casos em que alguém verificou que existe evidência
de desperdício, baseline real e mecanismo de cobrança conhecido. Um `rule_id`
fora dela não aceita estimativa contextual nenhuma, e esvaziar o mapa desliga a
funcionalidade inteira sem tocar em código.

**Nada daqui entra no portfólio.** `Maturity.CONTEXTUAL_ESTIMATE` e
`include_in_portfolio=False` são constantes neste caminho, não decisões.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from julius.collection.models import Account
from julius.config import Config
from julius.findings.investigation import AIContextualEstimate, ContextualEstimate
from julius.findings.maturity import Maturity
from julius.findings.signal import Signal
from julius.knowledge.billing_mechanisms import known_mechanisms
from julius.knowledge.rules.glue.estimation import window_baseline

DOMINIO_OFICIAL = "docs.aws.amazon.com"


@dataclass(frozen=True)
class GenerativeCandidate:
    """Um sinal para o qual a faixa contextual é permitida, e por quê."""

    rule_id: str
    #: Chave em `billing_mechanisms`. A unidade cobrada precisa ser conhecida.
    mechanism: str
    #: De onde sai o custo do ativo. `glue_job` ou `sagemaker_job`.
    baseline_source: str
    #: Por que nenhuma fórmula determinística fecha este caso. Sem esta frase a
    #: entrada não existe — ela é o que impede a lista de crescer por hábito.
    why_no_formula: str


#: A lista piloto. Três casos, escolhidos por terem o baseline mais confiável e o
#: mecanismo de cobrança mais direto. Crescer daqui exige a mesma frase que as
#: três já têm, e um eval que mostre a faixa sendo reproduzida à mão.
_ELEGIVEIS: dict[str, GenerativeCandidate] = {
    "GLUE-CODE-PYTHON-UDF": GenerativeCandidate(
        rule_id="GLUE-CODE-PYTHON-UDF",
        mechanism="glue_dpu_hour",
        baseline_source="glue_job",
        why_no_formula=(
            "o custo da UDF depende da cardinalidade, do plano e do que ela faz "
            "por linha; nenhuma métrica do Glue separa o tempo gasto nela"
        ),
    ),
    "GLUE-CODE-DRIVER-MATERIALIZATION": GenerativeCandidate(
        rule_id="GLUE-CODE-DRIVER-MATERIALIZATION",
        mechanism="glue_dpu_hour",
        baseline_source="glue_job",
        why_no_formula=(
            "o custo depende do volume materializado, que só é conhecido em "
            "execução; a mesma chamada é correta sobre cem linhas e ruinosa "
            "sobre cem milhões"
        ),
    ),
    "SM-CODE-FIXED-EPOCHS": GenerativeCandidate(
        rule_id="SM-CODE-FIXED-EPOCHS",
        mechanism="sagemaker_instance_second",
        baseline_source="sagemaker_job",
        why_no_formula=(
            "quantas épocas seriam dispensáveis depende da curva de validação, "
            "que o script não declara e a telemetria de treino não expõe"
        ),
    ),
}


def eligible(rule_id: str) -> GenerativeCandidate | None:
    return _ELEGIVEIS.get(rule_id)


def eligible_rule_ids() -> tuple[str, ...]:
    return tuple(sorted(_ELEGIVEIS))


def baseline_for(
    account: Account, signal: Signal, config: Config
) -> tuple[float | None, str]:
    """O custo do ativo, resolvido pelo motor. Nunca pela análise.

    Devolve `(None, motivo)` quando não há custo — e `None` é resposta melhor que
    zero, que se leria como "não há o que ganhar aqui".
    """
    candidato = eligible(signal.rule_id)
    if candidato is None:
        return None, "rule_id não elegível a estimativa contextual"
    if candidato.baseline_source == "glue_job":
        job = account.job_by_name(signal.asset_name)
        if job is None:
            return None, "job não existe no inventário"
        valor, fonte, _ = window_baseline(job, config.pricing)
        if valor <= 0:
            return None, "DPU-hora observada do job na janela"
        return valor, f"DPU-hora da janela × {fonte}"
    treino = next(
        (item for item in account.sagemaker_jobs if item.name == signal.asset_name),
        None,
    )
    if treino is None:
        return None, "training job não existe no inventário"
    custo = (
        treino.allocated_cost
        if treino.allocated_cost is not None
        else treino.modeled_cost
    )
    if not custo or custo <= 0:
        return None, "custo observado ou modelado do job"
    return float(custo), (
        "custo atribuído do job"
        if treino.allocated_cost is not None
        else "custo modelado pela tarifa da instância"
    )


def evaluate_contextual(
    account: Account,
    signal: Signal,
    proposta: AIContextualEstimate,
    config: Config,
) -> ContextualEstimate:
    """As sete condições da faixa contextual, verificadas antes de aceitá-la."""
    candidato = eligible(signal.rule_id)
    if candidato is None:
        raise ValueError(
            f"{signal.rule_id} não aceita estimativa contextual; elegíveis: "
            f"{', '.join(eligible_rule_ids()) or 'nenhum'}"
        )

    faltando: list[str] = []
    if proposta.billing_mechanism != candidato.mechanism:
        faltando.append(
            f"mecanismo de cobrança {proposta.billing_mechanism!r} não corresponde "
            f"a {signal.rule_id} (esperado {candidato.mechanism!r})"
        )
    elif proposta.billing_mechanism not in known_mechanisms():
        faltando.append(f"mecanismo desconhecido: {proposta.billing_mechanism}")
    if not proposta.reasoning.strip():
        faltando.append("faixa sem raciocínio declarado")
    if not proposta.inputs:
        faltando.append("faixa sem entradas nomeadas")
    if not proposta.validation_plan:
        # Sem caminho para medir, a faixa nunca sai de faixa.
        faltando.append("faixa sem plano de validação")
    if not proposta.assumptions:
        faltando.append("faixa sem premissa declarada")
    oficiais = [
        url
        for url in proposta.documentation
        if urlparse(url).scheme == "https" and urlparse(url).hostname == DOMINIO_OFICIAL
    ]
    if not oficiais:
        faltando.append(
            f"faixa sem documentação oficial em {DOMINIO_OFICIAL}"
        )

    baseline, fonte = baseline_for(account, signal, config)
    if baseline is None:
        faltando.append(fonte)

    if faltando:
        return ContextualEstimate(
            method=f"generative_contextual_{candidato.mechanism}_v1",
            status="needs_evidence",
            baseline_cost=baseline,
            maturity=Maturity.CONTEXTUAL_ESTIMATE,
            missing_evidence=[*proposta.missing_evidence, *faltando],
            pricing_region=config.pricing.region,
            currency=config.pricing.currency,
            method_version="v1",
            evidence_hash=signal.evidence_signature(),
        )

    assert baseline is not None  # garantido pelo gate acima
    # O teto é do motor, não da proposta: uma faixa acima do custo do próprio
    # ativo exigiria custo downstream comprovado, e comprová-lo é outro cálculo.
    teto = min(float(proposta.high), baseline)
    esperado = min(float(proposta.expected), teto)
    baixo = min(float(proposta.low), esperado)
    return ContextualEstimate(
        method=f"generative_contextual_{candidato.mechanism}_v1",
        status="estimated",
        baseline_cost=round(baseline, 2),
        estimated_low=round(baixo, 2),
        estimated_expected=round(esperado, 2),
        estimated_high=round(teto, 2),
        maturity=Maturity.CONTEXTUAL_ESTIMATE,
        include_in_portfolio=False,
        assumptions=[
            *proposta.assumptions,
            f"baseline {fonte}, resolvido pelo motor",
            f"cobrança por {candidato.mechanism}",
            f"raciocínio: {proposta.reasoning}",
        ],
        missing_evidence=[
            *proposta.missing_evidence,
            *(
                ["faixa reduzida ao baseline do ativo"]
                if float(proposta.high) > baseline
                else []
            ),
            *[f"validar: {item}" for item in proposta.validation_plan],
        ],
        pricing_region=config.pricing.region,
        currency=config.pricing.currency,
        method_version="v1",
        evidence_hash=signal.evidence_signature(),
    )
