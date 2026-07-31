"""Quem é o dono, quando a conta não usa a chave de tag que o Julius esperava.

Sete pontos da coleta liam tag de dono em três formas diferentes, e os sete
tinham o mesmo par de defeitos: uma **única** chave, comparada com **caixa
exata**. Conta padronizada em `owner`, `Squad` ou `CostCenter` ficava inteira
sem responsável.

Isso não é cosmético: `check_actionable` exige dono ou ator, e `assign()` trata
`owner is None` como bloqueador de `fazer_agora`. Sem dono, o achado nasce em
`investigar_primeiro` por falta de responsável — não por falta de evidência.

Nada aqui altera cifra. Resolver dono move achado de fila.
"""

from __future__ import annotations

import pytest

from julius.collection.models import Account, Table
from julius.collection.ownership_tags import owner_from_tags
from julius.graph.ownership import owner_from_name, resolve_owner

# --------------------------------------------------------------------------
# Tag, nas duas formas em que a AWS a devolve
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags",
    [
        pytest.param({"Owner": "squad-dados"}, id="dicionário"),
        pytest.param(
            [{"Key": "Owner", "Value": "squad-dados"}], id="lista Key/Value"
        ),
    ],
)
def test_both_shapes_of_tag_resolve_the_owner(tags):
    """Glue devolve dicionário; SageMaker e Redshift devolvem lista."""
    assert owner_from_tags(tags) == "squad-dados"


@pytest.mark.parametrize(
    "chave", ["Owner", "owner", "OWNER", "Squad", "team", "TIME", "CostCenter"]
)
def test_the_key_is_matched_without_case_or_separator(chave):
    """`cost_center`, `CostCenter` e `cost-center` são a mesma chave."""
    assert owner_from_tags({chave: "squad-dados"}) == "squad-dados"


def test_the_separator_in_the_key_does_not_matter():
    assert owner_from_tags({"cost_center": "fin"}) == "fin"
    assert owner_from_tags({"cost-center": "fin"}) == "fin"


def test_owner_wins_over_cost_center():
    """`Owner` declara dono; `CostCenter` é rateio contábil e vem por último."""
    tags = {"CostCenter": "fin-1234", "Owner": "squad-dados"}

    assert owner_from_tags(tags) == "squad-dados"


def test_an_empty_value_is_not_an_owner():
    assert owner_from_tags({"Owner": "   ", "Squad": "squad-dados"}) == "squad-dados"


@pytest.mark.parametrize("tags", [None, {}, [], "texto", 42])
def test_anything_that_is_not_a_tag_collection_resolves_to_nothing(tags):
    assert owner_from_tags(tags) is None


# --------------------------------------------------------------------------
# Convenção de nome
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nome", "esperado"),
    [
        ("squad-vendas-etl-diario", "squad-vendas"),
        ("SQUAD_Pagamentos_job", "squad_pagamentos"),
        ("team-ml-treino", "team-ml"),
        ("tribo-risco-carga", "tribo-risco"),
        ("etl-diario-vendas", None),
        ("", None),
    ],
)
def test_the_name_convention_is_the_last_resort(nome, esperado):
    assert owner_from_name(nome) == esperado


def test_a_tag_always_beats_the_name_convention():
    """Nome é intenção de quem criou o recurso, não declaração de propriedade."""
    conta = Account(
        account_id="1",
        tables=[Table(name="squad-vendas-resumo", owner_tag="squad-financeiro")],
    )

    atribuicao = resolve_owner(conta, "table", "squad-vendas-resumo")

    assert atribuicao.owner == "squad-financeiro"
    assert atribuicao.confidence == 1.0


def test_the_name_resolves_the_owner_when_nothing_else_does():
    conta = Account(account_id="1", tables=[Table(name="squad-vendas-resumo")])

    atribuicao = resolve_owner(conta, "table", "squad-vendas-resumo")

    assert atribuicao.owner == "squad-vendas"
    # Abaixo de tag (1.0), CloudTrail (0.9/0.85) e comunidade (0.6).
    assert atribuicao.confidence == 0.4
    assert "convenção de nome" in atribuicao.source


def test_a_name_without_convention_stays_unknown():
    """Inventar dono seria pior que não ter: manda a ação para quem não é."""
    conta = Account(account_id="1", tables=[Table(name="resumo_regional")])

    atribuicao = resolve_owner(conta, "table", "resumo_regional")

    assert atribuicao.owner is None
    assert atribuicao.confidence == 0.0
