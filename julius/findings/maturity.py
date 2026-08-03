"""Quando um número já pode ser somado, e quando ainda não.

Havia quatro vocabulários para isso e nenhuma relação declarada entre eles:
`ContextualEstimate.status` (`estimated`/`needs_evidence`/`rejected`),
`Investigation.status` (`candidate`/`needs_evidence`/`rejected`),
`PotentialRange.quality` (sempre `potential`) e `EvidenceQuality` (seis níveis).
Cada um respondia uma pergunta legítima; nenhum respondia *esta*.

**Maturidade não é o mesmo que qualidade de evidência, e confundi-las é o erro
caro.** `EvidenceQuality` responde "quão forte é a evidência que sustenta este
número". `Maturity` responde "este número já pode entrar no total oficial". Uma
estimativa pode ter evidência forte — baseline vindo da fatura reconciliada — e
ainda assim não poder ser somada, porque o cenário nunca foi testado. As duas
escalas são ortogonais de propósito, e fundi-las produziria uma terceira coisa
que não responde nenhuma das duas perguntas.

A ordem aqui é de amadurecimento, e ela só anda para frente com evidência nova.
A passagem para `VALIDATED_MODEL` exige, além disso, decisão humana registrada —
é o único ponto em que uma estimativa assistida encosta no dinheiro oficial.
"""

from __future__ import annotations

from enum import StrEnum


class Maturity(StrEnum):
    """Do mais cru ao observado. Só os dois últimos chegam perto do portfólio."""

    #: Ordem de grandeza para priorizar investigação. Custo do ativo vezes uma
    #: fração plausível do padrão — a fração é premissa, não medição.
    POTENTIAL = "potential"

    #: Baseline real e mecanismo de cobrança conhecido, sem fórmula que feche.
    #: Fica separada do total oficial até ser validada.
    CONTEXTUAL_ESTIMATE = "contextual_estimate"

    #: Cenário definido e calculável; faltam benchmark e comparação para saber
    #: se a premissa se sustenta neste ativo.
    PILOT_REQUIRED = "pilot_required"

    #: Fórmula e premissas confirmadas por piloto. Pode entrar no portfólio de
    #: forma condicional, com fator conservador e aprovação humana registrada.
    VALIDATED_MODEL = "validated_model"

    #: Resultado observado na conta depois da implementação externa.
    MEASURED = "measured"


#: O único estado que uma estimativa assistida pode alcançar sem piloto e sem
#: humano. Existe como constante porque a checagem aparece em mais de um lugar e
#: escrevê-la à mão nos dois seria a forma mais fácil de afrouxá-la em um só.
NUNCA_SOMA = frozenset(
    {Maturity.POTENTIAL, Maturity.CONTEXTUAL_ESTIMATE, Maturity.PILOT_REQUIRED}
)


def pode_entrar_no_portfolio(maturity: Maturity | str) -> bool:
    """`validated_model` e `measured`, e só. O resto é informação, não dinheiro."""
    return Maturity(maturity) not in NUNCA_SOMA
