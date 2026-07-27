"""Agrupamento por causa raiz.

Um mesmo ativo (ex.: um Glue Job) pode acionar várias regras. Sem agrupar, o
relatório/e-mail mostraria N ações redundantes. `group_by_asset` consolida os
achados do mesmo ativo num único item com **uma ação principal**, anexando os
demais como achados relacionados.

Conservador por design: o ganho do item agrupado é o do achado **primário**
(maior economia) — não somamos as economias dos achados relacionados, para não
superestimar (elas podem se sobrepor). Os relacionados ficam registrados como
evidência e podem ser detalhados depois.
"""

from __future__ import annotations

from collections import defaultdict

from julius.findings.opportunity import Opportunity


def _key(o: Opportunity) -> tuple[str, str, str]:
    return (o.account, o.asset_type, o.asset_name)


def group_by_asset(opportunities: list[Opportunity]) -> list[Opportunity]:
    buckets: dict[tuple, list[Opportunity]] = defaultdict(list)
    # Um achado promovido de sinal não é ação concorrente sobre o ativo: é uma
    # pergunta que a análise contextual sustentou, sem economia e bloqueada.
    # Agrupá-lo com uma ação determinística o transformaria em texto dentro da
    # lista de riscos de outro achado — perdendo o ID, a origem e o ciclo de
    # vida que a promoção existe para dar.
    grouped: list[Opportunity] = [
        o for o in opportunities if o.origin == "ai_confirmed"
    ]
    for o in opportunities:
        if o.origin != "ai_confirmed":
            buckets[_key(o)].append(o)

    for members in buckets.values():
        if len(members) == 1:
            grouped.append(members[0])
            continue

        # Primário = ação não bloqueada antes de investigação; dentro da mesma
        # classe, maior economia (empate por prioridade de execução).
        members.sort(
            key=lambda o: (
                not o.blocked,
                o.estimated_gain.monthly_expected,
                o.execution_priority,
            ),
            reverse=True,
        )
        primary, *others = members

        related = [f"{o.finding} ({o.rule_id})" for o in others]
        primary.evidence = list(primary.evidence) + [
            "Achados relacionados no mesmo ativo: " + "; ".join(related)
        ]
        # Preserva as recomendações relacionadas sem somar economias sobrepostas.
        primary.risks = list(primary.risks) + [
            f"Ação relacionada {o.rule_id}: {o.recommended_action}. "
            f"Validar: {o.how_to_validate}"
            for o in others
        ]
        plural = "achado relacionado" if len(others) == 1 else "achados relacionados"
        primary.finding = f"{primary.finding} (+{len(others)} {plural})"
        primary.data_sources = sorted({s for o in members for s in o.data_sources})
        grouped.append(primary)

    return grouped
