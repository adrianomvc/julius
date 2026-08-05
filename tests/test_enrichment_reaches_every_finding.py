"""Uma resposta por família, e ela alcança todos os achados daquela família.

O relatório mostrava o quadro do Devin em alguns achados e não em outros, sem
nada dizer onde passava a fronteira. Ela passava na posição dez: o pacote levava
`analysis.opportunities[:10]`, e o resto do portfólio nunca era enriquecido.

O desperdício que sustentava isso: quatro `GLUE-TIMEOUT-EXCESSIVE` são a mesma
correção e gastavam quatro das dez vagas para dizer a mesma coisa. Agrupando pela
família — que já estava carimbada em todo achado — o pacote encolhe e cobre tudo.
"""

from __future__ import annotations

from collections import Counter

import pytest

from julius.analysis import build_agent_context
from julius.pipeline import analyze

SAMPLE = "data/sample/consumer-avi.json"


@pytest.fixture(scope="module")
def analise():
    return analyze(SAMPLE)


# --- o recorte --------------------------------------------------------------


def test_every_family_gets_exactly_one_question(analise):
    pacote = build_agent_context(analise)

    familias = [item["remediation_family"] for item in pacote.opportunities]

    assert len(familias) == len(set(familias)), "família perguntada duas vezes"


def test_no_family_is_left_out(analise):
    pacote = build_agent_context(analise)

    no_pacote = {item["remediation_family"] for item in pacote.opportunities}
    no_portfolio = {o.remediation_family for o in analise.opportunities}

    assert no_portfolio <= no_pacote


def test_four_findings_of_one_family_become_one_question(analise):
    """O caso concreto que motivou a mudança."""
    contagem = Counter(o.remediation_family for o in analise.opportunities)
    repetida, quantas = contagem.most_common(1)[0]
    assert quantas > 1, "o dataset precisa ter família repetida para isto valer"

    pacote = build_agent_context(analise)
    perguntas = [
        item
        for item in pacote.opportunities
        if item["remediation_family"] == repetida
    ]

    assert len(perguntas) == 1
    assert len(perguntas[0]["applies_to"]) == quantas - 1


def test_the_representative_carries_the_siblings_it_speaks_for(analise):
    """Sem a lista, a análise responderia como se fosse um ativo só."""
    pacote = build_agent_context(analise)
    com_irmaos = [i for i in pacote.opportunities if i["applies_to"]]

    assert com_irmaos, "o dataset precisa ter família com mais de um achado"
    for irmao in com_irmaos[0]["applies_to"]:
        assert irmao["opportunity_id"]
        assert irmao["asset_name"]


def test_a_finding_without_a_known_family_is_still_asked_about():
    """Não classificado não pode virar não analisado.

    Sobre objetos próprios, e não sobre a fixture: zerar a família de um achado
    compartilhado mudaria o agrupamento visto pelos testes seguintes, e a
    contaminação apareceria como falha intermitente em outro arquivo.
    """
    from types import SimpleNamespace

    from julius.analysis.context_builder import _one_per_family

    grupos = _one_per_family(
        [
            SimpleNamespace(opportunity_id="A", remediation_family=""),
            SimpleNamespace(opportunity_id="B", remediation_family=""),
        ]
    )

    assert len(grupos) == 2, "dois sem família não podem ser fundidos num só"


def test_the_package_says_how_many_findings_it_covers(analise):
    """Onze perguntas cobrindo trinta achados não é onze de trinta."""
    pacote = build_agent_context(analise)

    assert pacote.portfolio["covered"] == pacote.portfolio["total_opportunities"]
    assert pacote.portfolio["analyzed"] < pacote.portfolio["covered"]


# --- o fan-out --------------------------------------------------------------


def _analise_contextual(analise, respostas):
    from julius.analysis.response_validator import ContextualAnalysis

    return ContextualAnalysis(
        account=analise.vm.account_id,
        scan_id=analise.vm.scan_id,
        executive_summary="resumo",
        recommendations=respostas,
        implementation_order=[r.opportunity_id for r in respostas],
    )


def _resposta(opportunity_id):
    from julius.analysis.response_validator import ContextualRecommendation

    return ContextualRecommendation(
        opportunity_id=opportunity_id,
        contextual_diagnosis="a causa provável",
        recommendation="a ação",
        implementation_steps=["passo um", "passo dois"],
        validation_steps=["conferir depois"],
        risks=["pode esconder gargalo"],
    )


def test_a_sibling_inherits_the_answer_and_says_where_it_came_from(analise):
    from julius.reporting.contextual import attach_contextual_analysis

    familia = Counter(o.remediation_family for o in analise.opportunities).most_common(1)[0][0]
    irmaos = [o for o in analise.vm.table if o.remediation_family == familia]
    assert len(irmaos) > 1

    vm = attach_contextual_analysis(
        analise.vm, _analise_contextual(analise, [_resposta(irmaos[0].id)])
    )
    representante = next(o for o in vm.table if o.id == irmaos[0].id)
    seguinte = next(o for o in vm.table if o.id == irmaos[1].id)

    assert representante.ai_diagnosis == "a causa provável"
    assert representante.ai_derived_from == "", "o representante não herdou de ninguém"

    assert seguinte.ai_diagnosis == "a causa provável", "o irmão ficou sem resposta"
    assert seguinte.ai_derived_from == representante.asset, (
        "o irmão precisa dizer em que ativo aquilo foi apurado"
    )


def test_coverage_counts_findings_and_not_questions(analise):
    from julius.reporting.contextual import attach_contextual_analysis

    familia = Counter(o.remediation_family for o in analise.opportunities).most_common(1)[0][0]
    representante = next(o for o in analise.vm.table if o.remediation_family == familia)

    vm = attach_contextual_analysis(
        analise.vm, _analise_contextual(analise, [_resposta(representante.id)])
    )

    assert vm.ai_coverage["answers"] == 1
    assert vm.ai_coverage["covered"] > 1, "uma resposta cobriu só um achado"


# --- o que o leitor vê ------------------------------------------------------


def _vm_com_resposta(analise):
    from julius.reporting.contextual import attach_contextual_analysis

    familia = Counter(o.remediation_family for o in analise.opportunities).most_common(1)[0][0]
    representante = next(o for o in analise.vm.table if o.remediation_family == familia)
    return attach_contextual_analysis(
        analise.vm, _analise_contextual(analise, [_resposta(representante.id)])
    )


def test_the_design_renders_the_steps(analise):
    """O passo a passo existia no report.json e não chegava ao HTML."""
    from julius.reporting import renderer

    html = renderer.render_html(_vm_com_resposta(analise))

    assert "Passo a passo" in html
    assert "passo um" in html and "passo dois" in html
    assert "Como validar" in html and "conferir depois" in html
    assert "Riscos" in html and "pode esconder gargalo" in html


def test_the_design_shows_where_a_reused_answer_came_from(analise):
    from julius.reporting import renderer

    html = renderer.render_html(_vm_com_resposta(analise))

    assert "Apurado em" in html


def test_no_orphan_heading_without_an_answer():
    """Achado sem resposta da IA não pode render título vazio.

    Com análise própria: `attach_contextual_analysis` altera o view model no
    lugar, então a fixture do módulo já chega aqui enriquecida pelos testes
    anteriores — e o vazio que este teste precisa observar não existiria.
    """
    from julius.reporting import renderer

    html = renderer.render_html(analyze(SAMPLE).vm)

    assert "Passo a passo" not in html
    assert "Apurado em" not in html


def test_the_deterministic_how_to_stays_next_to_the_ai_block(analise):
    """Os dois se complementam: um é daquele ativo, o outro é da família.

    Se o bloco da IA tivesse substituído o determinístico, o cartão perderia os
    números apurados naquele ativo — que é justamente o que o fan-out não pode
    prometer.
    """
    from julius.reporting import renderer

    html = renderer.render_html(_vm_com_resposta(analise))
    determinístico = analise.vm.table[0].how_to_apply

    assert "Como aplicar" in html
    assert determinístico[:40] in html
