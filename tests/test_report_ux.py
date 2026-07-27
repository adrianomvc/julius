"""O relatório é lido por analistas de dados, não por quem escreveu o código.

Duas coisas quebravam isso, e ambas voltam sozinhas se ninguém as prender.

A primeira era repetição: o mesmo achado aparecia no foco 80/20, na tabela
priorizada, no detalhe das ações e ainda nos blocos de quick wins — quatro
vezes, medidas. Quem lia precisava reconciliar quatro versões do mesmo item
para descobrir que era um só.

A segunda era vocabulário. "Ganho × Confiança × Urgência ÷ Esforço",
`saving_quality`, `exec 45`, `sagemaker_app` — nada disso ajuda a decidir o que
fazer na segunda-feira.
"""

from __future__ import annotations

import re

from julius.pipeline import analyze
from julius.reporting import renderer

SAMPLE = "data/sample/consumer-avi.json"


def _html():
    return renderer.render_html(analyze(SAMPLE).vm)


def _visivel(html: str) -> str:
    """O que se lê sem abrir nada — fora dos `details` e antes do apêndice."""
    ate_apendice = html[: html.index("Detalhamento técnico")]
    return re.sub(r"<details.*?</details>", "", ate_apendice, flags=re.S)


def test_each_finding_appears_exactly_once():
    """Era 2,4× de inflação: 19 achados renderizados 46 vezes."""
    analysis = analyze(SAMPLE)
    html = renderer.render_html(analysis.vm)

    repetidos = {
        o.id: html.count(o.id) for o in analysis.vm.table if html.count(o.id) != 1
    }

    assert not repetidos, f"achado renderizado mais de uma vez: {repetidos}"


def test_the_reader_gets_the_problem_and_the_action_without_clicking():
    visivel = _visivel(_html())

    assert "O que fazer, em ordem" in visivel
    assert "O que fazer" in visivel, "a ação precisa estar no cartão"
    assert "ver detalhes" in _html(), "a evidência fica a um clique, não some"


def test_the_8020_survives_without_a_second_list():
    """O Pareto vira marcador na lista única, não uma lista paralela."""
    analysis = analyze(SAMPLE)
    visivel = _visivel(renderer.render_html(analysis.vm))

    assert "concentram a maior parte da economia" in visivel
    # Um chip por ação em foco, mais o da legenda que explica o que ele marca.
    assert visivel.count(">foco<") == len(analysis.vm.focus_ids) + 1
    # E a lista antiga não voltou.
    for antiga in ("Tabela priorizada", "Detalhe das ações", "Quick wins"):
        assert antiga not in visivel, f"seção duplicada de volta: {antiga}"


def test_the_visible_flow_speaks_to_an_analyst():
    """Fórmula de score e nome interno de tipo não ajudam a decidir nada."""
    visivel = _visivel(_html())

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
    encontrados = [termo for termo in proibidos if termo in visivel]

    assert not encontrados, (
        f"vocabulário interno no fluxo principal: {encontrados}. "
        "Ele pode ficar nos detalhes ou no apêndice, não no que se lê primeiro."
    )


def test_how_much_to_trust_the_number_is_visible():
    """Decide se o analista age hoje ou pede mais dado — não pode estar escondido."""
    visivel = _visivel(_html())

    assert (
        "valor medido na fatura" in visivel
        or "valor estimado a partir do consumo" in visivel
    )
    assert "confiança" in visivel and "esforço" in visivel


def test_the_technical_detail_stays_available_but_out_of_the_way():
    html = _html()

    assert "Detalhamento técnico" in html
    for tecnica in ("Saúde da coleta", "Athena — padrões", "Candidatos à Producer"):
        assert tecnica in html, f"{tecnica} sumiu do relatório"
        assert tecnica not in _visivel(html), f"{tecnica} está no caminho do leitor"


def test_the_recommendations_come_before_the_technical_appendix():
    html = _html()

    assert html.index("O que fazer, em ordem") < html.index("Detalhamento técnico")
