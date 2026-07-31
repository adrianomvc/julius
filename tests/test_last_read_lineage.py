"""Última leitura inferida da linhagem, e o que ela não pode sustentar.

O S3 não tem last access time por objeto, e as fontes que medem leitura de
verdade exigem configuração prévia no bucket. Sem nenhuma delas ligada, a única
data conhecida de um arquivo é a da última **escrita** — que não diz se o dado é
usado.

O histórico de query do Athena já cobria parte disso. Faltava a linhagem: se um
job declara ler a tabela A, a execução dele **é** uma leitura de A, mesmo sem
query nenhuma no Athena.

As duas origens afirmam coisas diferentes, e é isso que decide o que cada uma
destrava. Query observada diz **quando** o prefixo foi lido. Execução de job diz
que o dado **é consumido** — o job pode ler só a partição do dia, então a data
não é de leitura da parte inteira.
"""

from __future__ import annotations

from julius.collection.collectors.last_read import apply_last_read
from julius.collection.models import Account, AthenaQuery, GlueJob, S3Prefix, Table

ONTEM = "2026-07-30T03:00:00+00:00"
SEMANA_PASSADA = "2026-07-24T03:00:00+00:00"


def _conta(**extra) -> Account:
    base = {
        "account_id": "123456789012",
        "window_days": 30,
        "window_end": "2026-07-31",
        "tables": [Table(name="vendas", location="s3://lake/vendas/")],
        "s3_prefixes": [
            S3Prefix(
                bucket="lake",
                prefix="vendas/",
                kind="table_location",
                source_asset="vendas",
                object_count=100,
                total_bytes=1024,
            )
        ],
    }
    base.update(extra)
    return Account(**base)


def test_a_job_that_reads_the_table_counts_as_a_read():
    """Antes, tabela lida só por job ficava sem nenhuma data de leitura."""
    conta = _conta(
        glue_jobs=[
            GlueJob(name="agrega_vendas", reads_tables=["vendas"], last_run_at=ONTEM)
        ]
    )

    medidas = apply_last_read(conta)

    assert medidas == 1
    assert conta.tables[0].last_read_at == ONTEM
    assert conta.tables[0].last_read_source == "process_lineage"


def test_an_observed_query_wins_when_it_is_more_recent():
    """Data mais recente vence: leitura posterior é leitura posterior."""
    conta = _conta(
        glue_jobs=[
            GlueJob(name="agrega", reads_tables=["vendas"], last_run_at=SEMANA_PASSADA)
        ],
        athena_queries=[
            AthenaQuery(
                query_id="q1", reads_tables=["vendas"], last_execution_at=ONTEM
            )
        ],
    )

    apply_last_read(conta)

    assert conta.tables[0].last_read_at == ONTEM
    assert conta.tables[0].last_read_source == "catalog_read_history"


def test_the_lineage_wins_when_the_job_ran_later():
    conta = _conta(
        glue_jobs=[
            GlueJob(name="agrega", reads_tables=["vendas"], last_run_at=ONTEM)
        ],
        athena_queries=[
            AthenaQuery(
                query_id="q1",
                reads_tables=["vendas"],
                last_execution_at=SEMANA_PASSADA,
            )
        ],
    )

    apply_last_read(conta)

    assert conta.tables[0].last_read_at == ONTEM
    assert conta.tables[0].last_read_source == "process_lineage"


def test_the_prefix_carries_the_weaker_quality_of_the_inference():
    conta = _conta(
        glue_jobs=[
            GlueJob(name="agrega", reads_tables=["vendas"], last_run_at=ONTEM)
        ]
    )

    apply_last_read(conta)

    prefixo = conta.s3_prefixes[0]
    assert prefixo.last_read_at == ONTEM
    assert prefixo.access_source == "process_lineage"
    assert prefixo.access_quality == "process_inferred"


def test_a_job_that_declares_no_lineage_changes_nothing():
    conta = _conta(glue_jobs=[GlueJob(name="agrega", last_run_at=ONTEM)])

    assert apply_last_read(conta) == 0
    assert conta.tables[0].last_read_at == ""


def test_a_job_that_never_ran_is_not_a_read():
    conta = _conta(
        glue_jobs=[GlueJob(name="agrega", reads_tables=["vendas"], last_run_at="")]
    )

    assert apply_last_read(conta) == 0
    assert conta.tables[0].last_read_source == ""
