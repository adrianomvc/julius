"""Traduz o `ReportViewModel` para o contrato que o desenho declara.

O desenho pede `k.monthly`, `o.problem`, `s.costFmt`. O view model tem
`identified_fmt`, `why`, `cost_fmt`. São os mesmos dados com outros nomes, e
esta camada só renomeia e formata — não recalcula economia, não reordena, não
decide o que entra. Se ela começasse a decidir, o desenho passaria a depender
de regra de negócio escondida numa camada de apresentação, e a próxima versão
do designer quebraria coisas que ninguém liga a ele.

Duas exceções que valem explicação.

A primeira é `Markup`. O desenho passa CSS pronto em `style="{{ o.catChip }}"`.
Com autoescape ligado — e ele fica ligado, porque nome de recurso vem da conta
do cliente — isso sairia escapado e o estilo quebraria. Marcar só esses campos
como seguros resolve sem que o template ganhe um `| safe`, que seria uma
alteração no desenho.

A segunda é o corte entre `recsPrimary` e `recsMore`. O desenho previu que uma
conta real tem mais recomendação do que cabe numa página, e o corte usa o mesmo
foco 80/20 do resto do relatório — para não existirem duas noções de "o que
importa" no mesmo documento.
"""

from __future__ import annotations

from typing import Any

from markupsafe import Markup

from julius.reporting import formatters as fmt
from julius.reporting.arn import resource_id
from julius.reporting.view_models import OpportunityVM, ReportViewModel

#: Quantas recomendações ficam abertas antes do bloco "ver mais". O desenho
#: dimensiona o cartão primário para caber quatro sem rolagem infinita.
PRIMARY_MIN = 4


def _chip(fg: str, bg: str) -> Markup:
    """Uma declaração CSS completa, que o desenho insere em `style="…"`."""
    return Markup(
        f"font-size:10.5px;font-weight:700;border-radius:5px;padding:3px 9px;"
        f"color:{fg};background:{bg};"
    )


def _rank_colors(destaque: bool) -> tuple[Markup, Markup]:
    """A bolinha do ranking: laranja no foco, neutra no resto."""
    if destaque:
        return Markup("#e8730c"), Markup("#ffffff")
    return Markup("#eef0ea"), Markup("#7c8374")


def _recommendation(
    o: OpportunityVM, rank: int, destaque: bool, account: str, region: str
) -> dict[str, Any]:
    rank_bg, rank_fg = _rank_colors(destaque)
    return {
        "id": o.id,
        "rank": rank,
        "rankBg": rank_bg,
        "rankFg": rank_fg,
        "title": o.title,
        "catLabel": o.category_label,
        "catChip": _chip(o.category_fg, o.category_bg),
        "arn": resource_id(o.asset_type, o.asset, account, region),
        # O desenho separa o que está errado do que fazer a respeito; a entidade
        # já carregava os dois campos, e o relatório antigo os fundia num texto.
        "problem": o.why,
        "action": o.action,
        "gainMonthFmt": o.monthly_fmt,
        "gainYearFmt": o.year_fmt,
        # O desenho tem um chip de confiança e não tem onde dizer se o número
        # foi medido na fatura ou estimado a partir do consumo — e essa é a
        # diferença entre agir hoje e pedir mais dado. Os dois cabem no mesmo
        # chip: "Confiança Alta · valor medido na fatura". `anchored_in_billing`
        # é a distinção que muda a decisão; a escala fina de qualidade de
        # evidência fica no JSON, para quem for auditar.
        "conf": (
            f"{o.confidence_label} · "
            + (
                "valor medido na fatura"
                if o.anchored_in_billing
                else "valor estimado a partir do consumo"
            )
        ),
        "confChip": _chip(o.conf_fg, o.conf_bg),
        # O chip fica na mesma linha do título; "Esforço muito fácil" empurra
        # o título para a segunda linha sem dizer nada a mais.
        "effortLabel": o.difficulty_label,
        "effortChip": _chip(o.diff_fg, o.diff_bg),
        "resp": o.owner,
        "ownerSrc": o.owner_source,
        "evidence": list(o.evidence),
        "howTo": o.how_to_apply,
        "howValidate": o.how_to_validate,
        "docUrl": o.doc_url,
        "devinNote": o.ai_diagnosis,
        # De onde a resposta veio, quando não veio deste ativo. O desenho só
        # mostra a linha quando há o que declarar.
        "derivedFrom": o.ai_derived_from,
        # O passo a passo que a análise devolve e que o relatório nunca mostrou.
        # Numerado aqui e não no template: o desenho não conta, ele exibe.
        "aiSteps": [
            {"n": posicao, "text": passo}
            for posicao, passo in enumerate(o.ai_implementation_steps, start=1)
        ],
        "aiValidate": list(o.ai_validation_steps),
        "aiRisks": list(o.ai_risks),
    }


def build(vm: ReportViewModel, *, version: str) -> dict[str, Any]:
    """O contexto completo que o desenho consome."""
    # O cabeçalho mascara a conta; o ARN, não. Um ARN com a conta mascarada não
    # é um ARN — ninguém cola aquilo no console. Quem recebe o relatório opera a
    # própria conta, então a máscara do cabeçalho é cosmética, e é melhor dizer
    # isso aqui do que entregar um identificador que não funciona.
    account, region = vm.account_id, vm.region
    foco = set(vm.focus_ids)

    recomendacoes = [
        _recommendation(o, posicao, o.id in foco, account, region)
        for posicao, o in enumerate(vm.table, start=1)
    ]
    # O corte tem que passar **depois** do último item em foco, não depois de
    # `len(foco)` itens: a lista é ordenada por prioridade de execução e o foco
    # é o 80/20 por economia, então um item de foco pode estar em quinto. Cortar
    # por contagem escondia justamente um dos poucos que o relatório destaca.
    ultimo_foco = max(
        (item["rank"] for item in recomendacoes if item["id"] in foco), default=0
    )
    corte = max(PRIMARY_MIN, ultimo_foco)
    primarias, resto = recomendacoes[:corte], recomendacoes[corte:]

    return {
        "version": version,
        "m": {
            "account": vm.account_masked,
            "period": vm.period,
            "lookback": vm.lookback,
            "scanId": vm.scan_id,
        },
        "k": {
            "monthly": vm.identified_fmt,
            "yearEnd": vm.realizable_year_fmt,
            "monthsRemaining": vm.months_remaining,
            "paretoPct": f"{vm.pareto_pct}%",
            "paretoCount": vm.pareto_count,
            "paretoSum": vm.pareto_sum_fmt,
            "paretoSentence": vm.pareto_sentence,
        },
        "account": {
            "totalFmt": vm.account_total_fmt,
            "savingFmt": vm.account_saving_fmt,
            "savingPct": vm.account_saving_pct,
            "savingBarW": vm.account_bar_w,
            "services": [
                {
                    "name": s["name"],
                    "sub": s["sub"],
                    "costFmt": s["cost_fmt"],
                    "savingFmt": s["saving_fmt"],
                    "savingColor": Markup(s["saving_color"]),
                    "pctOpt": s["pct_opt"],
                    "barBaseW": Markup(s["bar_base_w"]),
                    "barSaveW": Markup(s["bar_save_w"]),
                }
                for s in vm.services
            ],
        },
        "paretoBar": [
            {
                "title": seg["title"],
                "w": Markup(seg["w"]),
                "color": Markup(seg["color"]),
            }
            for seg in vm.pareto_bar
        ],
        # O atalho do 80/20 aponta para o cartão pelo `id`, então os itens do
        # foco precisam carregar o mesmo `rank` que têm na lista completa.
        "focus": [item for item in recomendacoes if item["id"] in foco],
        "recsPrimary": primarias,
        "recsMore": resto,
        "hasMore": bool(resto),
        "moreCount": len(resto),
        "moreSum": _soma(resto, vm),
        "prevResults": [
            {
                "title": p.title,
                "asset": p.asset,
                "date": p.date,
                "predicted": p.predicted_fmt,
                "realized": p.realized_fmt,
                "precision": f"{p.precision}%",
                "precChip": _chip(p.prec_fg, p.prec_bg),
                "unit": p.unit,
            }
            for p in vm.previous_results
        ],
        "manifest": [{"k": item["k"], "v": item["v"]} for item in vm.manifest],
    }


def _soma(itens: list[dict], vm: ReportViewModel) -> str:
    """Soma formatada do que ficou no bloco recolhido.

    Somar texto já formatado seria frágil; a soma vem do valor bruto que o
    cartão carrega, e o formato, do mesmo formatador que o resto do relatório.
    """
    ids = {item["id"] for item in itens}
    return fmt.money(sum(o.monthly for o in vm.table if o.id in ids), vm.currency)
