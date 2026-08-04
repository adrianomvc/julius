"""Formato de tabela aberta: saber se já migrou antes de recomendar migrar.

O erro óbvio de uma recomendação de Iceberg é chegar a quem já é Iceberg. Sem um
campo que diga o formato, o Julius não tinha como saber — `Table` guardava
`location` e `storage_bytes`, e nada sobre `table_type`.

**Formato de tabela aberta e formato de arquivo são eixos ortogonais.** Uma tabela
Iceberg é feita de arquivos Parquet; `storage_format` responde a segunda pergunta
e não a primeira. Confundir os dois faria "já é Parquet" passar por "já migrou".
"""

from __future__ import annotations

from julius.collection.collectors.glue.jobs import _open_table_format
from julius.collection.models import Account, Table
from julius.config import DEFAULT_CONFIG
from julius.knowledge.remediation import CATALOG, FAMILIES
from julius.knowledge.rules.data import rules as data_rules

_GB = 1024**3


def _tabela(**overrides) -> Table:
    base = {
        "name": "db.vendas",
        "storage_bytes": 200 * _GB,
        "touches_90d": 40,
    }
    return Table(**{**base, **overrides})


def _sinais(*tabelas) -> list:
    conta = Account(account_id="123456789012", tables=list(tabelas))
    return [
        item
        for item in data_rules.signals(conta, DEFAULT_CONFIG)
        if item.rule_id == "GLUE-TABLE-FORMAT-REVIEW"
    ]


# --- a leitura do catálogo -------------------------------------------------


def test_iceberg_is_read_from_the_table_type():
    assert _open_table_format({"table_type": "ICEBERG"}, {}) == "ICEBERG"


def test_the_declaration_is_case_insensitive():
    assert _open_table_format({"table_type": "iceberg"}, {}) == "ICEBERG"


def test_hudi_can_come_only_from_the_serde():
    descritor = {"SerdeInfo": {"SerializationLibrary": "org.apache.hudi.hadoop.X"}}

    assert _open_table_format({}, descritor) == "HUDI"


def test_a_plain_hive_table_answers_empty():
    assert _open_table_format({"classification": "parquet"}, {}) == ""


def test_parquet_is_not_an_open_table_format():
    """O eixo ortogonal: arquivo Parquet não diz nada sobre formato de tabela."""
    descritor = {
        "SerdeInfo": {
            "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.X"
        }
    }

    assert _open_table_format({}, descritor) == ""


# --- a regra ---------------------------------------------------------------


def test_a_large_and_read_hive_table_raises_the_question():
    sinal = _sinais(_tabela())[0]

    assert sinal.asset_type == "table"
    assert sinal.asset_name == "db.vendas"
    assert "pruning" in sinal.question


def test_a_table_that_already_migrated_is_left_alone():
    """O erro óbvio, e o motivo de o campo existir."""
    assert _sinais(_tabela(open_table_format="ICEBERG")) == []


def test_a_small_table_is_left_alone():
    """Abaixo do limiar, reescrever custa mais do que qualquer pruning devolve."""
    assert _sinais(_tabela(storage_bytes=1 * _GB)) == []


def test_a_table_nobody_reads_is_left_alone():
    """Tabela grande e esquecida tem outra recomendação, e `DATA-UNUSED-OUTPUT`
    já a faz. Migrar formato de dado morto é trabalho sobre desperdício."""
    assert _sinais(_tabela(touches_90d=0)) == []


def test_it_never_becomes_money():
    conta = Account(account_id="123456789012", tables=[_tabela()])
    achados = data_rules.detect(conta, DEFAULT_CONFIG, "scan")

    assert all(item.rule_id != "GLUE-TABLE-FORMAT-REVIEW" for item in achados)


def test_it_carries_no_range():
    """Sem baseline de bytes varridos, `None` é a resposta — e não zero."""
    assert _sinais(_tabela())[0].potential_range is None


# --- a família -------------------------------------------------------------


def test_the_rule_has_its_own_family():
    assert CATALOG["GLUE-TABLE-FORMAT-REVIEW"] == "table_format"


def test_migrating_format_is_not_fixing_output_layout():
    """Fundir as duas juntaria "compactar arquivo pequeno", que mexe em quem
    escreve, com "reescrever a tabela", que muda todo consumidor."""
    assert CATALOG["GLUE-TABLE-FORMAT-REVIEW"] != CATALOG["GLUE-CODE-SMALL-FILES"]


def test_the_family_asks_for_a_pilot():
    familia = FAMILIES["table_format"]

    assert familia.resolved_by == "time"
    assert familia.effort == 4
