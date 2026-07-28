"""A política IAM publicada tem que cobrir o que o Julius chama.

`install/julius-readonly-policy.json` é o que o operador anexa ao permission set
do SSO. Se ela desatualizar, a falha não aparece aqui: aparece numa conta real,
como `permission_denied` no meio de uma coleta — e no caso do `Glue Jobs`, que é
a única fonte obrigatória, derruba o scan inteiro.

A allowlist de `test_read_only.py` é a lista autoritativa do que o produto pode
chamar. Estes testes prendem as duas juntas nos dois sentidos: operação nova sem
ação na política falha, e ação na política sem operação correspondente também.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_read_only import OPERACOES_PERMITIDAS

RAIZ = Path(__file__).resolve().parents[1]
POLITICA = RAIZ / "install" / "julius-readonly-policy.json"

#: Operações que não são chamadas AWS — são mecânica do boto3.
_NAO_SAO_OPERACOES = {"get_paginator"}

#: Operações invisíveis para a allowlist porque só são chamadas via paginador.
#: `get_paginator("get_partitions")` passa o nome como string, e o teste de
#: allowlist lê chamada de atributo. A política precisa delas mesmo assim.
_VIA_PAGINADOR = {"get_partitions"}


def _acoes() -> set[str]:
    documento = json.loads(POLITICA.read_text(encoding="utf-8"))
    return {
        acao
        for statement in documento["Statement"]
        for acao in statement["Action"]
    }


def _operacao(acao: str) -> str:
    """`glue:GetJobRuns` → `get_job_runs`."""
    nome = acao.split(":", 1)[1]
    saida: list[str] = []
    for indice, letra in enumerate(nome):
        if letra.isupper() and indice and not nome[indice - 1].isupper():
            saida.append("_")
        saida.append(letra.lower())
    return "".join(saida)


def test_the_policy_is_valid_json_with_a_single_read_only_statement():
    documento = json.loads(POLITICA.read_text(encoding="utf-8"))

    assert documento["Version"] == "2012-10-17"
    assert len(documento["Statement"]) == 1
    assert documento["Statement"][0]["Effect"] == "Allow"


def test_every_permitted_operation_has_an_action_in_the_policy():
    """Operação nova na allowlist sem ação na política é `permission_denied`
    numa conta real, não aqui."""
    operacoes_na_politica = {_operacao(acao) for acao in _acoes()}
    esperadas = OPERACOES_PERMITIDAS - _NAO_SAO_OPERACOES

    faltando = sorted(esperadas - operacoes_na_politica)

    assert faltando == [], (
        f"estas operações são permitidas e não estão na política: {faltando}. "
        f"Acrescente-as em {POLITICA.relative_to(RAIZ).as_posix()}"
    )


def test_the_policy_grants_nothing_the_allowlist_does_not_permit():
    """Política mais larga que a allowlist pede credencial que o produto não usa."""
    permitidas = OPERACOES_PERMITIDAS | _VIA_PAGINADOR

    sobrando = sorted(
        acao for acao in _acoes() if _operacao(acao) not in permitidas
    )

    assert sobrando == [], (
        f"a política concede o que o Julius não chama: {sobrando}. "
        "A credencial que o produto pede não deve ser maior que o uso dele."
    )


@pytest.mark.parametrize("acao", ["glue:GetJobs", "glue:GetJobRuns"])
def test_the_two_actions_that_abort_the_whole_scan_are_present(acao):
    """`Glue Jobs` é a única fonte obrigatória: sem ela não há scan."""
    assert acao in _acoes()


def test_no_action_in_the_policy_carries_a_mutation_verb():
    """A credencial que o produto pede não pode conceder poder de alterar."""
    from tests.test_read_only import _VERBOS_DE_MUTACAO

    agem = sorted(
        acao for acao in _acoes() if _VERBOS_DE_MUTACAO.match(_operacao(acao))
    )

    assert agem == [], f"verbo de mutação na política: {agem}"


def test_the_one_acting_operation_is_present_and_is_the_declared_exception():
    """`start_query` é isento do regex de mutação de propósito, não por acaso.

    Ele roda um SELECT no workgroup do Julius para ler a tabela de toques: não
    altera dado, mas custa bytes varridos e grava resultado em S3. A isenção
    está escrita no próprio regex (`start(?!_query)`), e é a única.
    """
    from tests.test_read_only import _VERBOS_DE_MUTACAO

    assert "athena:StartQueryExecution" in _acoes()
    assert _VERBOS_DE_MUTACAO.match("start_query_execution") is None
    assert _VERBOS_DE_MUTACAO.match("start_job_run") is not None


def test_the_documentation_points_at_the_policy():
    doc = (RAIZ / "docs" / "permissoes-aws.md").read_text(encoding="utf-8")

    assert "install/julius-readonly-policy.json" in doc
