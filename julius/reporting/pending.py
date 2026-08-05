"""A terceira pergunta do relatório: quanto eu ainda não sei, e quem responde.

As duas primeiras — o que faço primeiro, e o que resolve 80% — se respondem com o
portfólio. Esta se responde com as hipóteses, e ela só é útil se disser **quem
destrava cada uma**. Um número único — "potencial em investigação: US$ 3.100" —
chega ao usuário como conta a pagar, quando boa parte não custa nada ao time dele:

- `coleta` — falta uma fonte que o Julius sabe ler e não leu. Permissão IAM, flag,
  ou mais uma janela. Quando a métrica aparece, `_has_runtime_correlation` promove
  o sinal a oportunidade com cifra sozinho, e ninguém reprocessa nada;
- `analise` — a camada contextual lê o artefato inteiro e descarta ou confirma;
- `time` — execução controlada ou decisão de negócio. O único que consome sprint.

A linha de `coleta` é a que fecha o ciclo: quando a fonte que falta voltou parcial
ou indisponível na coleta, a saúde já gravou o próximo passo — *"validar
s3:GetObject e o --spark-event-logs-path dos jobs"*. A informação existia nos dois
lados e ninguém as juntou.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models import Account, CollectionHealth
from julius.findings.signal import Signal
from julius.knowledge.remediation import FAMILIES, unblocking_sources

#: Estados de fonte que explicam uma medição ausente. `not_applicable` fica de fora
#: de propósito: a fonte não se aplica ao perfil, então ela não deve nada.
_LACUNA = {"partial", "unavailable"}


@dataclass(frozen=True)
class PendingMeasurement:
    """Uma hipótese em aberto, com o próximo passo e quem o dá."""

    rule_id: str
    asset_type: str
    asset_name: str
    family: str
    family_label: str
    #: `coleta`, `analise` ou `time` — quem destrava **o próximo passo**, que pode
    #: ser mais barato que o dono terminal da família.
    unblocked_by: str
    effort: int
    next_action: str
    expected: float | None
    #: A fonte que voltou com lacuna e explica esta medição, quando existe uma.
    blocked_source: str = ""
    #: O que a saúde da coleta já disse que resolve aquela fonte.
    source_next_action: str = ""


@dataclass(frozen=True)
class PendingSummary:
    """O bloco inteiro, pronto para o relatório."""

    items: list[PendingMeasurement] = field(default_factory=list)
    #: Soma das faixas esperadas. É teto, nunca parcela a somar ao identificado —
    #: as duas saem do mesmo custo de ativo. Ver `findings/consolidation.py`.
    ceiling: float = 0.0
    by_owner: dict[str, float] = field(default_factory=dict)
    count_by_owner: dict[str, int] = field(default_factory=dict)

    @property
    def sentence(self) -> str:
        if not self.items:
            return "Nenhuma medição pendente: toda hipótese foi respondida."
        partes = [
            f"{self.count_by_owner.get(dono, 0)} com {dono}"
            for dono in ("coleta", "analise", "time")
            if self.count_by_owner.get(dono)
        ]
        return (
            f"{len(self.items)} medição(ões) pendente(s) — {', '.join(partes)}. "
            f"Até {self.ceiling:,.2f}/mês ainda não medido, e parte disso pode já "
            "estar na economia identificada."
        )


def _gaps_by_source(account: Account) -> dict[str, CollectionHealth]:
    return {
        item.source: item
        for item in account.collection_health
        if item.status in _LACUNA
    }


def _unblocker(signal: Signal, gaps: dict[str, CollectionHealth]) -> tuple[str, str, str]:
    """Quem dá o próximo passo, e a fonte que o explica quando há uma.

    A ordem é de custo crescente, e é o ponto: se uma fonte que responde este ativo
    voltou com lacuna, o passo mais barato é consertar a coleta — mesmo quando a
    família só se fecha com o time depois. Oferecer o benchmark antes da permissão
    IAM é mandar alguém medir o que o scan seguinte mediria de graça.
    """
    return unblocker_for(
        signal.asset_type,
        signal.remediation_family,
        gaps,
        from_code=signal.kind == "code",
    )


def unblocker_for(
    asset_type: str,
    family_id: str,
    gaps: dict[str, CollectionHealth],
    *,
    from_code: bool = False,
) -> tuple[str, str, str]:
    """Quem destrava, a partir do ativo e da família — sem depender de `Signal`.

    Sinal e achado com cifra bloqueada fazem a **mesma** pergunta sobre a mesma
    conta: o que falta para isto virar número, e quem consegue. Duas cópias desta
    cascata divergiriam no dia em que uma fonte mudasse de nome, e a que
    divergisse ficaria calada — não errada, calada, que é pior.
    """
    for fonte in unblocking_sources(asset_type):
        entrada = gaps.get(fonte)
        if entrada is not None:
            return "coleta", fonte, entrada.next_action or ""
    familia = FAMILIES.get(family_id)
    if from_code and familia is not None and familia.resolved_by == "time":
        # Sinal de código com o artefato em mãos: a leitura pode descartá-lo sem
        # ninguém medir nada, e descartar é metade do valor.
        return "analise", "", ""
    return (familia.resolved_by if familia is not None else "time"), "", ""


def build(account: Account, signals: list[Signal]) -> PendingSummary:
    gaps = _gaps_by_source(account)
    items: list[PendingMeasurement] = []
    for signal in signals:
        familia = FAMILIES.get(signal.remediation_family)
        dono, fonte, proximo = _unblocker(signal, gaps)
        items.append(
            PendingMeasurement(
                rule_id=signal.rule_id,
                asset_type=signal.asset_type,
                asset_name=signal.asset_name,
                family=signal.remediation_family,
                family_label=familia.label if familia else "",
                unblocked_by=dono,
                effort=signal.measurement_effort,
                next_action=signal.next_action,
                expected=(
                    signal.potential_range.expected if signal.potential_range else None
                ),
                blocked_source=fonte,
                source_next_action=proximo,
            )
        )

    by_owner: dict[str, float] = {}
    count_by_owner: dict[str, int] = {}
    for item in items:
        count_by_owner[item.unblocked_by] = count_by_owner.get(item.unblocked_by, 0) + 1
        by_owner[item.unblocked_by] = by_owner.get(item.unblocked_by, 0.0) + (
            item.expected or 0.0
        )
    return PendingSummary(
        items=items,
        ceiling=round(sum(item.expected or 0.0 for item in items), 2),
        by_owner={chave: round(valor, 2) for chave, valor in by_owner.items()},
        count_by_owner=count_by_owner,
    )



@dataclass(frozen=True)
class BlockedReason:
    """Por que um achado saiu sem cifra, e quem consegue destravá-lo."""

    #: O que falta, na frase que a própria regra escreveu.
    missing: str
    #: `coleta`, `analise` ou `time`.
    unblocked_by: str
    #: A fonte que voltou com lacuna e explica a falta, quando existe uma.
    blocked_source: str = ""
    #: O que a saúde da coleta já disse que resolve aquela fonte.
    source_next_action: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "missing": self.missing,
            "unblocked_by": self.unblocked_by,
            "blocked_source": self.blocked_source,
            "source_next_action": self.source_next_action,
        }


#: Prefixo que `_zero_unproven` e as regras usam para dizer o que faltou. Ler a
#: premissa é feio, e a alternativa era pior: um campo estruturado novo em
#: `Estimation` que oito regras teriam de preencher, contra uma frase que todas
#: já escrevem. Quando a segunda regra precisar de estrutura, o campo se paga.
_MARCA_DE_FALTA = "economia não quantificada"


def _o_que_falta(opportunity) -> str:
    """A frase que a regra escreveu sobre o que faltou, ou a evidência ausente."""
    estimation = getattr(opportunity, "estimation", None)
    for premissa in getattr(estimation, "assumptions", []) or []:
        if premissa.startswith(_MARCA_DE_FALTA):
            return premissa
    faltando = list(getattr(opportunity, "missing_evidence", []) or [])
    return "; ".join(faltando) if faltando else "evidência não declarada pela regra"


def blocked_reason(account: Account, opportunity) -> BlockedReason | None:
    """Por que este achado não tem cifra. `None` quando ele tem.

    O gate é `include_in_portfolio`, e não `blocked`: um achado estratégico ou de
    economia zero também chega ao leitor sem número, e a pergunta dele é a mesma.
    """
    if opportunity.include_in_portfolio:
        return None
    dono, fonte, proximo = unblocker_for(
        opportunity.asset_type,
        getattr(opportunity, "remediation_family", ""),
        _gaps_by_source(account),
    )
    return BlockedReason(
        missing=_o_que_falta(opportunity),
        unblocked_by=dono,
        blocked_source=fonte,
        source_next_action=proximo,
    )


__all__ = [
    "BlockedReason",
    "PendingMeasurement",
    "PendingSummary",
    "blocked_reason",
    "build",
    "unblocker_for",
]
