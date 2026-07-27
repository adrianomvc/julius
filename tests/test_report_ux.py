"""O relatório é lido por analistas de dados, não por quem escreveu o código.

Estes testes vieram de uma versão anterior do relatório e continuam valendo,
porque o que eles prendem não é o layout — é o que estragava a leitura e volta
sozinho se ninguém prender:

- **repetição**: o mesmo achado aparecia no foco 80/20, na tabela priorizada, no
  detalhe das ações e nos quick wins — quatro vezes, medidas;
- **vocabulário**: "Ganho × Confiança × Urgência ÷ Esforço", `saving_quality`,
  `sagemaker_app` — nada disso ajuda a decidir o que fazer na segunda-feira;
- **ação escondida**: o problema visível e a ação atrás de um clique.

O documento mudou — agora é o relatório desenhado — então as asserções falam do
que ele mostra. O apêndice técnico saiu do HTML de propósito: é material de
auditoria e vive no JSON, verificado em `test_pipeline` e nos testes de cada
família de regra.
"""

from __future__ import annotations

import re

from markupsafe import escape

from julius.pipeline import analyze
from julius.reporting import design_view, renderer

SAMPLE = "data/sample/consumer-avi.json"


def _html():
    return renderer.render_html(analyze(SAMPLE).vm)


def test_each_finding_is_detailed_exactly_once():
    """Era 2,4× de inflação: 19 achados renderizados 46 vezes.

    O índice do 80/20 aponta para os cartões com âncora — dois usos do mesmo
    `id`, um em cada ponta do link. O que não pode repetir é o **conteúdo**:
    problema, ação, evidência.
    """
    analysis = analyze(SAMPLE)
    html = renderer.render_html(analysis.vm)

    # O texto chega escapado — nome de recurso com apóstrofo vira `&#39;`. É o
    # autoescape funcionando, e a contagem tem que comparar o que está na página.
    def escrito(texto: str) -> int:
        return html.count(str(escape(texto)))

    repetidos = {o.id: escrito(o.why) for o in analysis.vm.table if o.why and escrito(o.why) != 1}

    assert not repetidos, f"o problema do achado foi escrito mais de uma vez: {repetidos}"


def test_the_reader_gets_the_problem_and_the_action_without_clicking():
    html = _html()

    assert "O problema" in html
    assert "O que fazer" in html
    assert "Recomendações, por prioridade" in html


def test_the_8020_is_an_index_into_the_list_not_a_second_list():
    """O Pareto vira atalho para o cartão, não uma cópia dele."""
    analysis = analyze(SAMPLE)
    html = renderer.render_html(analysis.vm)

    assert "Foco 80/20" in html
    for o in analysis.vm.table:
        if o.id in analysis.vm.focus_ids:
            assert f'href="#{o.id}"' in html, f"{o.id} em foco sem atalho para o cartão"

    for antiga in ("Tabela priorizada", "Detalhe das ações", "Quick wins"):
        assert antiga not in html, f"seção duplicada de volta: {antiga}"


def test_the_report_speaks_to_an_analyst():
    """Fórmula de score e nome interno de tipo não ajudam a decidir nada."""
    html = _html()

    proibidos = [
        "Ganho × Confiança",
        "saving_quality",
        "baseline_quality",
        "modeled_rule",
        "sagemaker_app",
        "s3_prefix",
        "state_machine",
        "redshift_cluster",
        "opportunity_id",
    ]
    encontrados = [termo for termo in proibidos if termo in html]

    assert not encontrados, f"vocabulário interno no relatório: {encontrados}"


def test_how_much_to_trust_the_number_is_visible():
    """Decide se o analista age hoje ou pede mais dado — não pode estar escondido.

    O desenho tem um chip de confiança e nenhum lugar para a procedência do
    número; os dois entram no mesmo chip, sem alterar o template.
    """
    html = _html()

    assert "valor medido na fatura" in html or "valor estimado a partir do consumo" in html
    assert "Confiança" in html
    assert "Esforço" in html or "Muito fácil" in html or "Fácil" in html


def test_the_long_tail_is_collapsed_not_dropped():
    """Uma conta real tem mais achado do que cabe numa página aberta."""
    vm = analyze(SAMPLE).vm
    contexto = design_view.build(vm, version="teste")
    html = renderer.render_html(vm)

    assert contexto["hasMore"], "o dataset de exemplo deveria exercitar o 'ver mais'"
    assert f"Ver mais {contexto['moreCount']} recomendações" in html
    # Recolhido é diferente de removido: o conteúdo está no HTML.
    for item in contexto["recsMore"]:
        assert item["title"] in html


def test_the_recommendations_come_before_the_supporting_context():
    html = _html()

    assert html.index("Recomendações, por prioridade") < html.index("Contexto e validação")


def test_the_manifest_says_how_the_report_was_made():
    """A auditoria começa por aqui — e é a única parte técnica que ficou."""
    html = _html()

    assert "Como este relatório foi gerado" in html
    assert not re.findall(r"\{\{", html)
