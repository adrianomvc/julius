"""O Julius só lê o catálogo da conta que ele está analisando.

Numa conta Consumer do Data Mesh o Glue Catalog enxerga bancos compartilhados
por outras contas. Percorrê-los custa um `get_tables` por banco e devolve
tabelas sobre as quais esta conta não pode agir. Este arquivo cobra as duas
coisas: que o filtro exista, e que ele aconteça **antes** do `get_tables` — um
filtro aplicado depois economiza memória e não economiza tempo, que é o
problema.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from julius.collection import sources as collect_module
from julius.collection.collectors.glue import jobs
from julius.collection.models import Account
from julius.collection.scope import CatalogScope
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG

_MESH = [
    "dbcompartilhado_consumer-avi",
    "dbcompartilhado_consumer-nova",
    "dbcompartilhado_consumer-atlas",
    "default",
]


def _glue():
    return boto3.client(
        "glue",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


# --------------------------------------------------------------------------
# A regra, sem AWS
# --------------------------------------------------------------------------


def test_the_account_suffix_keeps_only_its_own_shared_database():
    scope = CatalogScope(account_name="consumer-avi")

    assert scope.select(_MESH) == ["dbcompartilhado_consumer-avi"]


def test_separators_and_case_do_not_decide_anything():
    """`consumer_avi` no cadastro e `Consumer-AVI` no banco são a mesma conta."""
    scope = CatalogScope(account_name="consumer_avi")

    assert scope.select(["dbCompartilhado_Consumer-AVI"]) == [
        "dbCompartilhado_Consumer-AVI"
    ]


def test_a_prefix_match_is_not_a_suffix_match():
    """A conta vem no fim do nome; casar em qualquer posição pegaria vizinhos."""
    scope = CatalogScope(account_name="avi")

    assert scope.select(["avi_dbcompartilhado", "db_avi"]) == ["db_avi"]


def test_an_explicit_list_wins_over_the_naming_rule():
    """A saída de emergência para ambiente sem a convenção de nome."""
    scope = CatalogScope(
        account_name="consumer-avi", databases=("default", "dbcompartilhado_consumer-nova")
    )

    assert scope.select(_MESH) == ["dbcompartilhado_consumer-nova", "default"]


def test_no_scope_declared_keeps_every_database():
    """Comportamento antigo preservado — e declarado como tal em `rule`."""
    scope = CatalogScope()

    assert scope.select(_MESH) == _MESH
    assert scope.declared is False
    assert "todos os bancos" in scope.rule


# --------------------------------------------------------------------------
# O filtro acontece antes do get_tables
# --------------------------------------------------------------------------


def test_tables_are_read_only_from_the_databases_in_scope():
    """O Stubber é a prova: um `get_tables` a mais aqui é uma exceção.

    O Stubber falha quando o código faz uma chamada que não foi enfileirada.
    São enfileirados `get_databases` e **um** `get_tables`, do banco da conta —
    se o coletor tocasse nos outros três bancos do mesh, este teste quebraria.
    """
    glue = _glue()
    stub = Stubber(glue)
    stub.add_response(
        "get_databases",
        {"DatabaseList": [{"Name": name} for name in _MESH]},
        {},
    )
    stub.add_response(
        "get_tables",
        {
            "TableList": [
                {
                    "Name": "vendas",
                    "Parameters": {"julius:written_by": "agrega_vendas", "Owner": "squad-avi"},
                }
            ]
        },
        {"DatabaseName": "dbcompartilhado_consumer-avi"},
    )
    stub.activate()

    names = jobs.list_database_names(glue)
    chosen = CatalogScope(account_name="consumer-avi").select(names)
    tables = jobs.collect_tables(glue, chosen)

    stub.assert_no_pending_responses()
    assert [table.name for table in tables] == ["dbcompartilhado_consumer-avi.vendas"]
    assert tables[0].written_by == "agrega_vendas"
    assert tables[0].owner_tag == "squad-avi"


# --------------------------------------------------------------------------
# O recorte aparece na saúde da coleta
# --------------------------------------------------------------------------


def _context(scope: CatalogScope, glue) -> collect_module.CollectionContext:
    class _Session:
        region_name = "sa-east-1"

        def client(self, name, **_kwargs):
            assert name == "glue"
            return glue

    return collect_module.CollectionContext(
        session=_Session(),
        window=AnalysisWindow.trailing(),
        billing=BillingMonth.current(),
        account=Account(account_id="123456789012"),
        config=DEFAULT_CONFIG,
        catalog_scope=scope,
    )


def _stubbed_catalog(glue, *, tables_for: list[str]):
    stub = Stubber(glue)
    stub.add_response(
        "get_databases", {"DatabaseList": [{"Name": name} for name in _MESH]}, {}
    )
    for name in tables_for:
        stub.add_response("get_tables", {"TableList": []}, {"DatabaseName": name})
    stub.activate()
    return stub


def test_the_scope_publishes_how_many_databases_it_left_out():
    """Menos tabelas por escopo e menos tabelas por permissão se parecem."""
    glue = _glue()
    _stubbed_catalog(glue, tables_for=["dbcompartilhado_consumer-avi"])
    ctx = _context(CatalogScope(account_name="consumer-avi"), glue)

    collect_module._collect_catalog(ctx)

    entry = ctx.pending_health[-1]
    assert entry.source == "Glue Catalog Scope"
    assert entry.status == "ok"
    assert (entry.collected, entry.expected) == (1, 4)
    assert entry.impact == "bancos terminados em 'consumer-avi'"
    assert entry.affects_status is False


def test_a_scope_that_matches_nothing_is_partial_and_says_what_it_looked_for():
    glue = _glue()
    _stubbed_catalog(glue, tables_for=[])
    ctx = _context(CatalogScope(account_name="conta-que-nao-existe"), glue)

    assert collect_module._collect_catalog(ctx) == []

    entry = ctx.pending_health[-1]
    assert entry.status == "partial"
    assert entry.error_category == "no_data"
    assert "conta-que-nao-existe" in entry.next_action


def test_scanning_the_whole_mesh_is_allowed_but_never_silent():
    """Sem escopo o comportamento é o antigo — e a saúde diz que foi isso."""
    glue = _glue()
    _stubbed_catalog(glue, tables_for=_MESH)
    ctx = _context(CatalogScope(), glue)

    collect_module._collect_catalog(ctx)

    entry = ctx.pending_health[-1]
    assert entry.status == "partial"
    assert entry.error_category == "not_configured"
    assert entry.collected == entry.expected == 4
    assert "--account-name" in entry.next_action


@pytest.mark.parametrize("suffix", ["consumer-avi", "CONSUMER_AVI", "consumeravi"])
def test_the_registry_name_matches_however_it_is_written(suffix):
    assert CatalogScope(account_name=suffix).select(_MESH) == [
        "dbcompartilhado_consumer-avi"
    ]
