"""Carregar só o playbook do que está no pacote.

`SCOPE` era uma tupla com dez blocos e ia inteira em todo briefing. Uma conta sem
Redshift recebia as perguntas de Redshift; uma sem SageMaker recebia as seis de
SageMaker. O custo não é só de contexto: um bloco em que a maior parte não se
aplica ensina quem lê a passar os olhos, e a pergunta que importava viaja junto
com nove que não importavam.

O recorte é por `asset_type` presente — em oportunidade ou em sinal, porque as
perguntas servem tanto para julgar a hipótese quanto para enriquecer o achado.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from julius.analysis.playbook import (
    CROSS_SERVICE,
    PLAYBOOKS,
    asset_types_in_context,
    known_asset_types,
    load_playbooks,
    render,
)

RAIZ = Path(__file__).resolve().parents[1]


def test_a_package_loads_only_the_assets_it_contains():
    """A afirmação central da onda, no menor caso possível."""
    saida = render({"glue_job"})

    assert "\nglue_job:\n" in saida
    for ausente in known_asset_types() - {"glue_job"}:
        assert f"\n{ausente}:\n" not in saida


def test_the_cut_actually_cuts():
    """Contraprova de tamanho: sem ela, "carrega só o necessário" é afirmação vazia.

    Se o recorte não reduzisse, todos os testes acima continuariam verdes com um
    `render` que ignora o argumento.
    """
    completo = render(None)
    recortado = render({"glue_job"})

    assert len(recortado) < len(completo) / 2, (
        f"o recorte precisa reduzir de verdade: {len(recortado)} de {len(completo)}"
    )


def test_an_empty_package_asks_nothing():
    """Sem ativo conhecido não há pergunta — nem um bloco vazio de rodapé."""
    assert render(set()) == ""


def test_sections_inside_one_file_are_cut_independently():
    """Agrupar por serviço no arquivo não pode significar carregar o serviço todo.

    `sagemaker.md` tem app, training job e endpoint. Uma conta só com endpoint
    recebe endpoint — senão o agrupamento por arquivo, que existe para quem
    edita, viraria imprecisão para quem lê.
    """
    saida = render({"sagemaker_endpoint"})

    assert "\nsagemaker_endpoint:\n" in saida
    assert "\nsagemaker_app:\n" not in saida
    assert "\nsagemaker_training_job:\n" not in saida


def test_cross_service_enters_by_rule_not_by_asset():
    """Achado da relação entre dois ativos não tem `asset_type` próprio."""
    tipos = asset_types_in_context(
        [{"asset_type": "glue_job", "rule_id": "XSVC-WASTED-PRODUCTION"}], []
    )

    assert CROSS_SERVICE in tipos

    sem_cross = asset_types_in_context(
        [{"asset_type": "glue_job", "rule_id": "GLUE-OVERPROVISIONED"}], []
    )
    assert CROSS_SERVICE not in sem_cross


def test_a_signal_alone_is_enough_to_load_its_playbook():
    """Sinal é o caso em que a pergunta mais importa; não pode ficar sem ela."""
    tipos = asset_types_in_context([], [{"asset_type": "state_machine", "rule_id": "X"}])

    assert tipos == {"state_machine"}
    assert "\nstate_machine:\n" in render(tipos)


def test_every_playbook_section_carries_questions():
    """Seção vazia é playbook pela metade, e some do briefing sem avisar."""
    vazias = [
        f"{playbook.name}:{asset}"
        for playbook in load_playbooks()
        for asset, perguntas in playbook.sections.items()
        if not perguntas
    ]

    assert not vazias, f"seção de playbook sem perguntas: {vazias}"


def test_the_playbooks_ship_with_the_package():
    """`docs/` não entra no wheel; isto entra, e precisa estar declarado.

    É a classe de erro que a suíte não pega por construção — os testes rodam do
    repositório, onde todo arquivo existe. `tests/test_package_data.py` cobre o
    caso geral; aqui a asserção é específica, porque um playbook ausente não
    quebra o Julius: ele monta um briefing pior, em silêncio.
    """
    config = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    declarado = config["tool"]["setuptools"]["package-data"]

    assert "playbooks/*.md" in declarado.get("julius.analysis", []), (
        "os playbooks precisam estar em package-data, ou o pacote instalado "
        "monta análise sem nenhuma pergunta por tipo de ativo"
    )
    assert sorted(PLAYBOOKS.glob("*.md")), "nenhum playbook no disco"


@pytest.mark.parametrize(
    "asset",
    sorted(known_asset_types()),
)
def test_each_known_asset_renders_its_own_block(asset):
    """Todo tipo declarado precisa ser alcançável — inclusive o cross-service."""
    saida = render({asset})

    assert f"\n{asset}:\n" in saida


def test_the_rendered_block_keeps_the_shape_the_briefing_expects():
    """O briefing concatena isto sob um cabeçalho; a forma faz parte do contrato."""
    saida = render({"glue_job"})

    assert saida.startswith("\nglue_job:\n- ")
    assert "\n\n\n" not in saida, "linha em branco dupla quebra a leitura do bloco"
