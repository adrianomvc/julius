"""Priorização: dois rankings, bloqueadores, buckets e gate de acionabilidade."""

from __future__ import annotations

from julius.findings.opportunity import Opportunity
from julius.findings.signal import Signal
from julius.scoring import impact

# Campos mínimos para uma oportunidade ser acionável (gate do Top 10).
_REQUIRED = (
    "asset_name",
    "finding",
    "recommended_action",
    "how_to_validate",
)


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


def execution_priority(o: Opportunity) -> int:
    """Ganho × Confiança × Urgência ÷ Esforço."""
    return _clamp(o.gain_score * o.confidence * o.urgency / max(1, o.difficulty_score))


def strategic_priority(o: Opportunity, risk: float) -> int:
    """Ganho × Confiança × Risco de não agir (0–1.2)."""
    return _clamp(o.gain_score * o.confidence * risk)


def investigation_priority(signal: Signal, config) -> int:
    """Ordem de investigar: faixa esperada ÷ esforço de medir.

    Mesma forma de `execution_priority` — retorno sobre esforço — porque a pergunta
    é a mesma: o que rende mais pelo que custa. O que muda é o custo medido. Ali é
    o de aplicar a mudança; aqui é o de **descobrir se ela vale**, e os dois se
    invertem com frequência: um endpoint ocioso é caro de desligar e barato de
    medir, porque o CloudWatch já respondeu.

    Sinal sem faixa devolve zero e não some da lista — `investigation_ranking_key`
    o ordena pelo esforço, que é a única informação que ele tem. Zero aqui significa
    "não sei quanto vale", nunca "não vale nada".
    """
    if signal.potential_range is None:
        return 0
    retorno = impact.gain_score(signal.potential_range.expected, config)
    return _clamp(retorno / max(1, signal.measurement_effort))


def investigation_ranking_key(signal: Signal, config) -> tuple:
    """Ordem da lista de medições pendentes, com desempate determinístico.

    Quatro níveis, todos "maior é melhor", para usar com `reverse=True`:

    1. **tem faixa** — hipótese com ordem de grandeza vem antes da que não tem
       nenhuma, porque a segunda não dá para dimensionar;
    2. **retorno ÷ esforço**;
    3. **esforço invertido** — entre duas sem faixa, a mais barata de medir
       primeiro; é o único critério que sobra e é o certo, porque medir barato é o
       que produz a faixa que falta;
    4. **regra e ativo**, para a lista não mudar de ordem entre dois scans com a
       mesma entrada.
    """
    return (
        signal.potential_range is not None,
        investigation_priority(signal, config),
        -signal.measurement_effort,
        signal.rule_id,
        signal.asset_name,
    )


def check_actionable(o: Opportunity) -> tuple[bool, str | None]:
    """Gate: precisa de evidência, ação, validação e responsável ou próximo passo."""
    for field_name in _REQUIRED:
        if not getattr(o, field_name):
            return False, "coletar evidência"
    if not o.evidence:
        return False, "coletar evidência"
    if o.owner is None and o.actor is None:
        return False, "identificar responsável"
    return True, None


def assign(
    o: Opportunity, *, risk: float = 0.6, blocked: bool | None = None
) -> None:
    """Preenche prioridades, gate e bucket na oportunidade (in place)."""
    if blocked is not None:
        o.blocked = blocked
    o.execution_priority = execution_priority(o)
    o.strategic_priority = strategic_priority(o, risk)

    actionable, next_action = check_actionable(o)
    o.actionable = actionable
    o.next_action = next_action

    if o.blocked:
        o.actionable = False
        o.next_action = o.next_action or "validar evidência antes de implementar"
        o.bucket = "investigar_primeiro"
        return

    low_conf = o.confidence < 0.55
    is_strategic = o.estimated_gain.is_strategic

    if not actionable:
        # Potencialmente valioso mas sem evidência suficiente.
        o.bucket = "investigar_primeiro"
        return

    if is_strategic:
        o.bucket = "preparar"
        return

    if low_conf:
        # Pode valer a pena, mas exige mais dados antes de agir.
        o.bucket = "investigar_primeiro" if o.gain_score >= 50 else "monitorar"
        return

    blockers = o.blocked or o.owner is None
    if o.difficulty_score <= 2 and o.execution_priority >= 45 and not blockers:
        o.bucket = "fazer_agora"
    elif o.execution_priority >= 25:
        o.bucket = "planejar"
    else:
        o.bucket = "monitorar"


# Ordem de desempate (todas "maior é melhor" após ajuste de sinal).
def tiebreak_key(o: Opportunity) -> tuple:
    return (
        o.estimated_gain.realizable_year,
        o.confidence,
        -len(o.risks),            # menor risco operacional
        -o.difficulty_score,      # menor dificuldade
        o.gain_score,
    )


def ranking_key(o: Opportunity) -> tuple:
    """Ordem de implantação, usada em todo lugar que mostra uma lista de ações.

    Três níveis, todos "maior é melhor", para usar com `reverse=True`:

    1. **Entra no portfólio.** Sem isto, item de cifra não validada — inclusive
       de valor zero — encabeçava a tabela, porque `execution_priority` não
       conhece o gate: o ganho de um estratégico é fixo em 92 e a dificuldade
       dele costuma ser 1, então ele ganha de economia medida com dificuldade 2.
       Ordenar não esconde nada; só para de sugerir que o não validado vem antes.
    2. **`execution_priority`**, que é o composto valor × confiança × urgência ÷
       dificuldade — e não o valor puro. Ação cara e difícil não vem antes de
       ação barata e imediata só por somar mais.
    3. **Desempate determinístico**, terminando no `opportunity_id` — que é
       estável entre scans, porque deriva de regra e ativo, não do scan. Sem ele
       a chave não é ordem total: itens empatados em tudo ficavam na ordem em que
       chegaram, e a mesma conta produzia listas diferentes conforme a ordem de
       avaliação das regras. Alfabético invertido é arbitrário, e é o ponto —
       arbitrário e estável.

    O corte financeiro do Pareto continua por valor: ali a pergunta é "quantas
    ações somam 80% da economia", e responder isso exige as maiores primeiro.
    """
    return (
        o.include_in_portfolio,
        o.execution_priority,
        *tiebreak_key(o),
        o.opportunity_id,
    )
