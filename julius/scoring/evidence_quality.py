"""Uma escala única para "quanto confiar neste número".

Havia três vocabulários paralelos, cada um com nomes próprios, e nenhuma relação
declarada entre eles:

- `cost_quality`     — `reconciled` / `partial` / `unavailable`
- `baseline_quality` — `allocated` / `allocated_partial` / `modeled`
- `saving_quality`   — `measured` / `modeled_evidence` / `modeled_rule`

Cada um respondia uma pergunta diferente e legítima: a cobrança fecha? o
baseline vem de fatura ou de tarifa? a economia foi medida ou inferida? O que
faltava era poder comparar dois achados — um com baseline alocado e economia
modelada por regra, outro com baseline modelado e economia medida. Sem uma
escala comum, "qual dos dois é mais confiável" não tinha resposta.

`EvidenceQuality` é essa escala. Ela **não substitui** os três campos: cada um
continua respondendo a sua pergunta, e a escala é derivada deles. Rebaixar é
sempre seguro; promover, nunca — o elo mais fraco decide.
"""

from __future__ import annotations

from enum import IntEnum


class EvidenceQuality(IntEnum):
    """Do mais forte para o mais fraco. A ordem numérica é o ponto.

    Comparar `EvidenceQuality` funciona: `MEASURED > MODELED` é verdadeiro, e é
    o que permite ordenar achados por confiança sem consultar três campos.
    """

    #: Antes e depois medidos na conta. Só existe após uma validação humana.
    REALIZED = 5
    #: Contrafactual medido — duplicatas exatas, bytes evitados observados.
    MEASURED = 4
    #: Ancorado na cobrança real, rateada e reconciliada.
    ALLOCATED = 3
    #: Ancorado na cobrança, mas com a reconciliação incompleta.
    ALLOCATED_PARTIAL = 2
    #: Tarifa versionada aplicada a consumo medido.
    MODELED = 1
    #: Faixa de regra sobre um baseline — o mais fraco que ainda vira número.
    MODELED_RULE = 0

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def is_anchored_in_billing(self) -> bool:
        """A fatura sustenta este número, ainda que parcialmente?"""
        return self >= EvidenceQuality.ALLOCATED_PARTIAL


_LABELS = {
    EvidenceQuality.REALIZED: "Realizado",
    EvidenceQuality.MEASURED: "Medido",
    EvidenceQuality.ALLOCATED: "Alocado",
    EvidenceQuality.ALLOCATED_PARTIAL: "Alocado parcial",
    EvidenceQuality.MODELED: "Modelado",
    EvidenceQuality.MODELED_RULE: "Modelado por regra",
}

# Como cada vocabulário existente se projeta na escala.
_BASELINE = {
    "allocated": EvidenceQuality.ALLOCATED,
    "allocated_partial": EvidenceQuality.ALLOCATED_PARTIAL,
    "modeled": EvidenceQuality.MODELED,
}
_SAVING = {
    "realized": EvidenceQuality.REALIZED,
    "measured": EvidenceQuality.MEASURED,
    "modeled_evidence": EvidenceQuality.MODELED,
    "modeled_rule": EvidenceQuality.MODELED_RULE,
    # Sem contrafactual, a economia não é número — o achado é investigação.
    "unavailable": EvidenceQuality.MODELED_RULE,
}
_COST = {
    "reconciled": EvidenceQuality.ALLOCATED,
    "partial": EvidenceQuality.ALLOCATED_PARTIAL,
    "unavailable": EvidenceQuality.MODELED,
}


def from_baseline(value: str) -> EvidenceQuality:
    return _BASELINE.get(value, EvidenceQuality.MODELED_RULE)


def from_saving(value: str) -> EvidenceQuality:
    return _SAVING.get(value, EvidenceQuality.MODELED_RULE)


def from_cost(value: str) -> EvidenceQuality:
    return _COST.get(value, EvidenceQuality.MODELED)


def combined(baseline: str, saving: str) -> EvidenceQuality:
    """A qualidade de um achado é a do seu elo mais fraco.

    Um baseline vindo da fatura não torna confiável uma economia estimada por
    faixa de regra, e vice-versa: quem lê o número precisa saber o pior dos
    dois, não a média nem o melhor.
    """
    return min(from_baseline(baseline), from_saving(saving))
