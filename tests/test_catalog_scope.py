"""O Julius só lê o catálogo da conta que ele está analisando.

Numa conta Consumer do Data Mesh o Glue Catalog enxerga bancos compartilhados
por outras contas. Percorrê-los custa um `get_tables` por banco e devolve
tabelas sobre as quais esta conta não pode agir. Este arquivo cobra as duas
coisas: que o filtro exista, e que ele aconteça **antes** do `get_tables` — um
filtro aplicado depois economiza memória e não economiza tempo, que é o
problema.

São três os bancos da conta, e eles não têm a mesma forma: o compartilhado
carrega o nome da conta, `workspace_db` e `sagemaker_featurestore` são fixos.
Uma regra de sufixo pegaria só o primeiro.
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

_DA_CONTA = "database_db_compartilhado_consumer_avi"

#: Um catálogo como a conta o enxerga: o dela, os fixos, e o mesh inteiro.
_MESH = [
    _DA_CONTA,
    "workspace_db",
    "sagemaker_featurestore",
    "database_db_compartilhado_consumer_nova",
    "database_db_compartilhado_consumer_atlas",
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


def test_the_three_databases_of_the_account_are_kept_and_the_mesh_is_not():
    escolhidos = CatalogScope(account_name="avi").select(_MESH)

    assert escolhidos == [_DA_CONTA, "workspace_db", "sagemaker_featurestore"]


def test_the_fixed_databases_would_be_lost_by_a_suffix_rule():
    """`workspace_db` não termina com o nome de conta nenhuma — e é da conta."""
    escolhidos = CatalogScope(account_name="avi").select(
        ["workspace_db", "sagemaker_featurestore"]
    )

    assert escolhidos == ["workspace_db", "sagemaker_featurestore"]


def test_a_neighbours_database_does_not_pass_for_ours():
    """`consumer_navi` termina em `avi`; o separador é o que impede o engano."""
    vizinho = "database_db_compartilhado_consumer_navi"

    assert CatalogScope(account_name="avi").select([vizinho, _DA_CONTA]) == [_DA_CONTA]


@pytest.mark.parametrize("conta", ["avi", "AVI", "consumer-avi", "CONSUMER_AVI"])
def test_the_registry_name_matches_however_it_is_written(conta):
    """O cadastro às vezes chama a conta de `consumer-avi`, às vezes de `avi`."""
    assert CatalogScope(account_name=conta).select(_MESH)[0] == _DA_CONTA


@pytest.mark.parametrize("conta", ["avi-prod", "consumer-avi-prod", "AVI_PROD"])
def test_the_environment_suffix_is_not_part_of_the_shared_database(conta):
    """O cadastro nomeia o ambiente; o banco compartilhado não."""
    assert CatalogScope(account_name=conta).select(_MESH)[0] == _DA_CONTA


def test_the_real_compact_account_name_matches_the_shared_database():
    database = "database_db_compartilhado_consumer_atendimentodataservice"

    assert CatalogScope(
        account_name="consumeratendimentodataservice-pro"
    ).select([database]) == [database]


def test_prod_inside_the_account_name_is_not_mistaken_for_an_environment():
    database = "database_db_compartilhado_consumer_produtos"

    assert CatalogScope(account_name="consumer-produtos").select([database]) == [database]


def test_case_and_separators_in_the_database_do_not_decide_anything():
    escritas = ["DATABASE_DB_COMPARTILHADO_CONSUMER_AVI", "workspace-db"]

    assert CatalogScope(account_name="avi").select(escritas) == escritas


def test_an_explicit_list_wins_over_the_naming_rule():
    """A saída de emergência para ambiente sem a convenção de nome."""
    escopo = CatalogScope(account_name="avi", databases=("default", "workspace_db"))

    assert escopo.select(_MESH) == ["workspace_db", "default"]


def test_no_scope_declared_keeps_every_database():
    """Comportamento antigo preservado — e declarado como tal em `rule`."""
    escopo = CatalogScope()

    assert escopo.select(_MESH) == _MESH
    assert escopo.declared is False
    assert "todos os bancos" in escopo.rule


def test_the_rule_names_the_three_databases_it_looked_for():
    """Quando nada casa, a saúde precisa dizer o que foi procurado."""
    regra = CatalogScope(account_name="avi").rule

    assert _DA_CONTA in regra
    assert "workspace_db" in regra
    assert "sagemaker_featurestore" in regra


# --------------------------------------------------------------------------
# O filtro acontece antes do get_tables
# --------------------------------------------------------------------------


def test_tables_are_read_only_from_the_databases_in_scope():
    """O Stubber é a prova: um `get_tables` a mais aqui é uma exceção.

    São enfileirados `get_databases` e **três** `get_tables`, um por banco da
    conta — se o coletor tocasse nos bancos do mesh, este teste quebraria.
    """
    glue = _glue()
    stub = Stubber(glue)
    stub.add_response(
        "get_databases", {"DatabaseList": [{"Name": nome} for nome in _MESH]}, {}
    )
    stub.add_response(
        "get_tables",
        {
            "TableList": [
                {
                    "Name": "vendas",
                    "Parameters": {
                        "julius:written_by": "agrega_vendas",
                        "Owner": "squad-avi",
                    },
                }
            ]
        },
        {"DatabaseName": _DA_CONTA},
    )
    for nome in ("workspace_db", "sagemaker_featurestore"):
        stub.add_response("get_tables", {"TableList": []}, {"DatabaseName": nome})
    stub.activate()

    nomes = jobs.list_database_names(glue)
    escolhidos = CatalogScope(account_name="avi").select(nomes)
    tabelas = jobs.collect_tables(glue, escolhidos)

    stub.assert_no_pending_responses()
    assert [tabela.name for tabela in tabelas] == [f"{_DA_CONTA}.vendas"]
    assert tabelas[0].written_by == "agrega_vendas"
    assert tabelas[0].owner_tag == "squad-avi"


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


def _stubbed_catalog(glue, *, tables_for: list[str], catalog: list[str] | None = None):
    stub = Stubber(glue)
    stub.add_response(
        "get_databases",
        {"DatabaseList": [{"Name": nome} for nome in (catalog or _MESH)]},
        {},
    )
    for nome in tables_for:
        stub.add_response("get_tables", {"TableList": []}, {"DatabaseName": nome})
    stub.activate()
    return stub


def test_the_scope_publishes_how_many_databases_it_left_out():
    """Menos tabelas por escopo e menos tabelas por permissão se parecem."""
    glue = _glue()
    _stubbed_catalog(
        glue, tables_for=[_DA_CONTA, "workspace_db", "sagemaker_featurestore"]
    )
    ctx = _context(CatalogScope(account_name="avi"), glue)

    collect_module._collect_catalog(ctx)

    entry = ctx.pending_health[-1]
    assert entry.source == "Glue Catalog Scope"
    assert entry.status == "ok"
    assert (entry.collected, entry.expected) == (3, 6)
    assert _DA_CONTA in entry.impact
    assert entry.affects_status is False


def test_a_scope_that_matches_nothing_is_partial_and_says_what_it_looked_for():
    """Só acontece quando o catálogo não tem nenhum dos três.

    Errar o nome da conta sozinho não zera o escopo: `workspace_db` e
    `sagemaker_featurestore` entram por nome fixo, independentemente dela.
    """
    glue = _glue()
    _stubbed_catalog(
        glue,
        tables_for=[],
        catalog=["database_db_compartilhado_consumer_nova", "default"],
    )
    ctx = _context(CatalogScope(account_name="conta-que-nao-existe"), glue)

    assert collect_module._collect_catalog(ctx) == []

    entry = ctx.pending_health[-1]
    assert entry.status == "partial"
    assert entry.error_category == "no_data"
    assert "conta_que_nao_existe" in entry.next_action


def test_scanning_the_whole_mesh_is_allowed_but_never_silent():
    """Sem escopo o comportamento é o antigo — e a saúde diz que foi isso."""
    glue = _glue()
    _stubbed_catalog(glue, tables_for=_MESH)
    ctx = _context(CatalogScope(), glue)

    collect_module._collect_catalog(ctx)

    entry = ctx.pending_health[-1]
    assert entry.status == "partial"
    assert entry.error_category == "not_configured"
    assert entry.collected == entry.expected == 6
    assert "--account-name" in entry.next_action
