"""Agrupamento por causa raiz.

Um mesmo ativo (ex.: um Glue Job) pode acionar várias regras. Sem agrupar, o
relatório/e-mail mostraria N ações redundantes. `group_by_asset` consolida os
achados da mesma **ação de remediação** num único item com uma ação principal,
anexando os demais como achados relacionados.

Conservador por design: o ganho do item agrupado é o do achado **primário**
(maior economia) — não somamos as economias dos achados relacionados, para não
superestimar (elas podem se sobrepor). Os relacionados ficam registrados como
evidência e podem ser detalhados depois.

**Por que a família entra na chave.** Agrupar só por ativo tratava toda regra sobre
um job como a mesma coisa, e ela não é: em `sync_parceiros`, migrar para Flex
(US$ 90/mês) e consertar o job que falha (US$ 44,66/mês) são duas ações, com dois
donos possíveis e dois dinheiros. Fundidas, a segunda virava uma linha de texto
dentro dos riscos da primeira e a economia dela desaparecia do portfólio.

A regra conservadora acima continua valendo — e agora ela sabe **quando** se
aplica. Sobreposição é dentro da família, porque é lá que duas regras descrevem a
mesma correção; entre famílias os mecanismos de cobrança são diferentes, e somar é
correto. O teto por processo em `scoring/process_cost.py::apply_conservative_caps`
é o que impede a soma entre famílias de ultrapassar o custo do próprio ativo.
"""

from __future__ import annotations

from collections import defaultdict

from julius.findings.opportunity import Opportunity


def _key(o: Opportunity) -> tuple[str, str, str, str, str]:
    # Achado sem família cai no próprio `rule_id`: ele não se funde com ninguém,
    # que é o comportamento seguro para uma regra que o catálogo ainda não
    # classificou. Usar `""` faria todas as não classificadas de um ativo virarem
    # uma ação só, misturando correções que ninguém declarou serem a mesma.
    #
    # A origem entra na chave para separar duas coisas que não podem se fundir: um
    # achado promovido de sinal tem ID, ciclo de vida e procedência próprios, e
    # absorvê-lo num achado determinístico o transformaria em texto dentro dos
    # riscos de outro — perdendo exatamente o que a promoção existe para dar. Entre
    # si eles se fundem normalmente: dois sinais confirmados da mesma correção sobre
    # o mesmo ativo são uma ação, e o piloto de um não vale pelo outro.
    return (
        o.account,
        o.asset_type,
        o.asset_name,
        o.remediation_family or f"?{o.rule_id}",
        o.origin,
    )


def group_by_asset(opportunities: list[Opportunity]) -> list[Opportunity]:
    buckets: dict[tuple, list[Opportunity]] = defaultdict(list)
    grouped: list[Opportunity] = []
    for o in opportunities:
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
