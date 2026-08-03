"""O que uma estimativa precisa respeitar para valer como estimativa.

As proibições existiam em prosa — no plano, no briefing, nos comentários. Prosa
não recusa nada. Aqui cada uma é uma checagem sobre a `ContextualEstimate` pronta,
rodada antes de o motor devolvê-la, e uma violação rebaixa o resultado em vez de
deixá-lo passar com aparência de conta feita.

**Por que rebaixar e não levantar exceção.** Uma estimativa que viola um limite
não é um erro de programação: é uma pergunta que ainda não pode ser respondida.
`ValueError` seria a resposta certa se o chamador tivesse errado — e ele não
errou, o cenário é que não fecha. Rebaixar para `needs_evidence` com o motivo na
lista é o que preserva a informação: alguém lendo o relatório vê *por que* aquele
sinal não virou número, em vez de não ver o sinal.

As três misturas — período, moeda e região — merecem nota. Nenhuma delas dá erro
em lugar nenhum: somar dólar com real, mês com trimestre ou tarifa de duas regiões
produz um número perfeitamente formado e errado, que ninguém detecta depois porque
o resultado não tem como dizer de onde veio. É por isso que a procedência entrou
no contrato antes de qualquer coisa neste arquivo poder verificá-la.
"""

from __future__ import annotations

from dataclasses import replace

from julius.findings.investigation import ContextualEstimate
from julius.findings.maturity import Maturity, pode_entrar_no_portfolio
from julius.knowledge.billing_mechanisms import mechanism_for_method

#: Períodos que o motor sabe comparar. Qualquer outro é mistura esperando
#: acontecer, porque a conversão exigiria uma premissa que ninguém declarou.
PERIODOS = frozenset({"monthly"})


def problems(estimate: ContextualEstimate, *, baseline_currency: str = "USD") -> list[str]:
    """Tudo que impede esta estimativa de ser tratada como número.

    Devolve lista, não booleano: quem chama precisa dizer **o que** faltou, e uma
    estimativa costuma violar mais de uma coisa de uma vez.
    """
    achados: list[str] = []

    if estimate.status != "estimated":
        # Sem número não há o que verificar; `needs_evidence` já é a resposta.
        return achados

    baseline = estimate.baseline_cost
    faixa = (estimate.estimated_low, estimate.estimated_expected, estimate.estimated_high)

    if baseline is None or baseline <= 0:
        achados.append("estimativa sem baseline positivo")
    if any(valor is None for valor in faixa):
        achados.append("faixa incompleta: low, expected e high são obrigatórios")
    else:
        low, expected, high = (float(valor) for valor in faixa)  # type: ignore[arg-type]
        if not low <= expected <= high:
            achados.append(
                f"faixa fora de ordem: {low} / {expected} / {high}"
            )
        if baseline is not None and baseline > 0 and high > baseline:
            # Economia maior que o custo do próprio ativo só faz sentido com
            # custo downstream comprovado, e comprová-lo é outro cálculo.
            achados.append(
                f"economia acima do baseline sem custo downstream comprovado: "
                f"{high} > {baseline}"
            )

    if not estimate.pricing_region:
        achados.append("tarifa sem região declarada")
    if not estimate.currency:
        achados.append("estimativa sem moeda declarada")
    elif estimate.currency != baseline_currency:
        achados.append(
            f"moeda da estimativa ({estimate.currency}) difere do baseline "
            f"({baseline_currency})"
        )
    if estimate.period not in PERIODOS:
        achados.append(
            f"período {estimate.period!r} não é comparável; use um de {sorted(PERIODOS)}"
        )
    if not estimate.method_version:
        achados.append("método sem versão: a conta deixa de ser reproduzível")
    if not estimate.evidence_hash:
        achados.append("estimativa sem assinatura de evidência: não há como invalidá-la")
    if mechanism_for_method(estimate.method) is None:
        achados.append(
            f"método {estimate.method!r} sem mecanismo de cobrança declarado"
        )
    if not estimate.assumptions:
        achados.append("estimativa sem premissa declarada")
    if pode_entrar_no_portfolio(estimate.maturity) and not estimate.include_in_portfolio:
        # Coerência interna: o estado diz que pode somar e a flag diz que não.
        achados.append(
            f"maturidade {estimate.maturity!r} contradiz include_in_portfolio=False"
        )
    if estimate.include_in_portfolio and not pode_entrar_no_portfolio(estimate.maturity):
        achados.append(
            f"include_in_portfolio=True com maturidade {estimate.maturity!r}"
        )
    return achados


def enforce(
    estimate: ContextualEstimate, *, baseline_currency: str = "USD"
) -> ContextualEstimate:
    """Devolve a estimativa, ou a versão rebaixada dela com o motivo anexado."""
    achados = problems(estimate, baseline_currency=baseline_currency)
    if not achados:
        return estimate
    return replace(
        estimate,
        status="needs_evidence",
        maturity=Maturity.PILOT_REQUIRED,
        include_in_portfolio=False,
        estimated_low=None,
        estimated_expected=None,
        estimated_high=None,
        missing_evidence=[*estimate.missing_evidence, *achados],
    )
