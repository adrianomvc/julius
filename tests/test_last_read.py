"""Última **leitura** não é última escrita — e o S3 só conhece a segunda.

`LastModified` diz quando o objeto foi gravado. Um arquivo escrito uma vez e
lido todo dia tem `LastModified` de um ano atrás, e mandá-lo para o Glacier a
partir disso troca a classe de dado quente: o time dono paga retrieval para
reverter, e a "economia" vira despesa.

As fontes que medem leitura de verdade — server access logs, Storage Lens
advanced, Storage Class Analysis — exigem configuração prévia no bucket. Este
módulo cobre o caso em que nenhuma está ligada, com o que o Julius já coleta:
o histórico de queries do Athena e a tabela oficial de toques.
"""

from __future__ import annotations

from julius.collection.collectors.last_read import (
    apply_last_read,
    last_read_by_prefix,
    last_read_for,
)
from julius.collection.models import Account, AthenaQuery, S3Prefix, Table


def _conta() -> Account:
    return Account(
        account_id="123456789012",
        tables=[
            Table(name="db.vendas", location="s3://lake/vendas/"),
            Table(name="db.arquivo_morto", location="s3://lake/arquivo/"),
        ],
        athena_queries=[
            AthenaQuery(
                query_id="a1",
                reads_tables=["db.vendas"],
                last_execution_at="2026-07-28T10:00:00+00:00",
            ),
            AthenaQuery(
                query_id="a2",
                reads_tables=["db.vendas"],
                last_execution_at="2026-06-01T10:00:00+00:00",
            ),
        ],
    )


def test_the_most_recent_read_wins_across_query_patterns():
    conta = _conta()

    medidas = apply_last_read(conta)

    assert conta.tables[0].last_read_at == "2026-07-28T10:00:00+00:00"
    assert medidas == 1


def test_a_table_no_query_read_stays_unmeasured_not_zero():
    """"Ninguém leu" e "não medimos" precisam continuar distinguíveis.

    Se a ausência virasse uma data antiga, a tabela pareceria fria por
    construção — e toda tabela fora do histórico do Athena viraria candidata a
    Glacier.
    """
    conta = _conta()

    apply_last_read(conta)

    assert conta.tables[1].last_read_at == ""


def test_the_catalog_is_case_insensitive_like_glue():
    conta = _conta()
    conta.athena_queries[0].reads_tables = ["DB.Vendas"]

    apply_last_read(conta)

    assert conta.tables[0].last_read_at == "2026-07-28T10:00:00+00:00"


def test_a_query_without_an_execution_date_does_not_erase_what_is_known():
    conta = _conta()
    conta.tables[0].last_read_at = "2026-07-29T00:00:00+00:00"
    conta.athena_queries = [
        AthenaQuery(query_id="a1", reads_tables=["db.vendas"], last_execution_at="")
    ]

    apply_last_read(conta)

    assert conta.tables[0].last_read_at == "2026-07-29T00:00:00+00:00"


def test_the_touches_table_is_not_overwritten_by_an_older_athena_read():
    """A tabela oficial de toques é a fonte melhor; o Athena só complementa."""
    conta = _conta()
    conta.tables[0].last_read_at = "2026-07-29T00:00:00+00:00"

    apply_last_read(conta)

    assert conta.tables[0].last_read_at == "2026-07-29T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Do catálogo para o S3
# ---------------------------------------------------------------------------


def test_the_prefix_inherits_the_read_date_of_the_table_that_occupies_it():
    conta = _conta()
    apply_last_read(conta)

    por_prefixo = last_read_by_prefix(conta)

    assert por_prefixo == {"s3://lake/vendas": "2026-07-28T10:00:00+00:00"}


def test_a_partition_inside_the_table_counts_as_read():
    """Ler a tabela é ler a partição: a location é a raiz, não a folha."""
    conta = _conta()
    apply_last_read(conta)
    por_prefixo = last_read_by_prefix(conta)

    particao = S3Prefix(bucket="lake", prefix="vendas/dt=2026-01-01/")

    assert last_read_for(particao, por_prefixo) == "2026-07-28T10:00:00+00:00"


def test_a_prefix_that_only_starts_alike_is_not_the_same_path():
    """`vendas-antigo/` não é `vendas/`, e sem a barra os dois casariam."""
    conta = _conta()
    apply_last_read(conta)
    por_prefixo = last_read_by_prefix(conta)

    outro = S3Prefix(bucket="lake", prefix="vendas-antigo/")

    assert last_read_for(outro, por_prefixo) == ""


def test_a_prefix_outside_the_catalog_is_unmeasured_not_unread():
    conta = _conta()
    apply_last_read(conta)
    por_prefixo = last_read_by_prefix(conta)

    logs = S3Prefix(bucket="lake", prefix="spark-logs/etl/")

    assert last_read_for(logs, por_prefixo) == ""


def test_an_account_without_queries_measures_nothing_and_does_not_crash():
    conta = Account(account_id="1", tables=[Table(name="db.x", location="s3://l/x/")])

    assert apply_last_read(conta) == 0
    assert last_read_by_prefix(conta) == {}
