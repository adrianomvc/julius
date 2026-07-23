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

from julius.opportunities.base import Opportunity


def _key(o: Opportunity) -> tuple[str, str, str]:
    return (o.account, o.asset_type, o.asset_name)


def group_by_asset(opportunities: list[Opportunity]) -> list[Opportunity]:
    buckets: dict[tuple, list[Opportunity]] = defaultdict(list)
    for o in opportunities:
        buckets[_key(o)].append(o)

    grouped: list[Opportunity] = []
    for members in buckets.values():
        if len(members) == 1:
            grouped.append(members[0])
            continue

        # Primário = maior economia mensal (empate por prioridade de execução).
        members.sort(
            key=lambda o: (o.estimated_gain.monthly_expected, o.execution_priority),
            reverse=True,
        )
        primary, *others = members

        related = [f"{o.finding} ({o.rule_id})" for o in others]
        primary.evidence = list(primary.evidence) + [
            "Achados relacionados no mesmo ativo: " + "; ".join(related)
        ]
        # Próximos passos após a ação principal (ações dos relacionados).
        primary.risks = list(primary.risks)
        primary.finding = f"{primary.finding} (+{len(others)} achados relacionados)"
        primary.data_sources = sorted({s for o in members for s in o.data_sources})
        grouped.append(primary)

    return grouped
