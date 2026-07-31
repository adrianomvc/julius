"""Reprocessamento medido, e as três condições que o impedem de mentir.

`bookmark_saving` exigia dois campos que nenhum coletor escrevia. A cifra agora
sai do cruzamento entre o que o job leu (CloudWatch) e o tamanho da fonte
(listagem S3) — mas só quando as três condições valem, porque cada uma delas,
sozinha, transforma a subtração em número inventado.
"""

from __future__ import annotations

from julius.collection.models import Account, GlueJob, S3Prefix
from julius.collection.redundant_reads import apply_redundant_reads

GB = 1024**3


def _prefixo(tabela: str, *, bytes_totais: int = 10 * GB, **extra) -> S3Prefix:
    base = {
        "bucket": "lake",
        "prefix": f"{tabela}/",
        "kind": "table_location",
        "source_asset": tabela,
        "total_bytes": bytes_totais,
        "object_count": 500,
        "listing_complete": True,
        "date_partitioned": True,
    }
    base.update(extra)
    return S3Prefix(**base)


def _job(*, le: list[str], lidos: float | None = 100 * GB) -> GlueJob:
    return GlueJob(
        name="agrega_vendas",
        job_bookmark=False,
        reads_tables=le,
        bytes_read_window=lidos,
    )


def _conta(job: GlueJob, prefixos: list[S3Prefix]) -> Account:
    return Account(
        account_id="123456789012", glue_jobs=[job], s3_prefixes=prefixos
    )


def test_reading_far_more_than_the_source_measures_the_reprocessing():
    """Com bookmark, uma passada lê a fonte uma vez; sem ele, relê a cada vez."""
    job = _job(le=["vendas"], lidos=100 * GB)

    medidos = apply_redundant_reads(_conta(job, [_prefixo("vendas")]))

    assert medidos == 1
    assert job.incremental_source_evidence is True
    assert job.redundant_read_bytes_window == 90 * GB


def test_a_source_rewritten_whole_is_not_reprocessing():
    """Reler tabela reescrita por inteiro é o único jeito de lê-la."""
    job = _job(le=["vendas"])

    apply_redundant_reads(
        _conta(job, [_prefixo("vendas", date_partitioned=False)])
    )

    assert job.incremental_source_evidence is False
    assert job.redundant_read_bytes_window is None


def test_an_unknown_source_blocks_the_measurement_entirely():
    """`bytes_read_window` é a soma de tudo que o job leu.

    Com uma fonte fora do inventário, a subtração atribuiria a leitura dela a
    reprocessamento que não houve.
    """
    job = _job(le=["vendas", "clientes"])

    apply_redundant_reads(_conta(job, [_prefixo("vendas")]))

    assert job.redundant_read_bytes_window is None
    # E nem a condição de fonte incremental é afirmada com inventário parcial.
    assert job.incremental_source_evidence is False


def test_a_truncated_listing_blocks_the_measurement():
    """Prefixo truncado dá tamanho que é piso; piso no subtraendo infla a conta."""
    job = _job(le=["vendas"])

    apply_redundant_reads(
        _conta(job, [_prefixo("vendas", listing_complete=False)])
    )

    assert job.incremental_source_evidence is True
    assert job.redundant_read_bytes_window is None


def test_without_observability_there_is_nothing_to_subtract_from():
    """`bytes_read_window` vem do CloudWatch de observabilidade, que pode estar off."""
    job = _job(le=["vendas"], lidos=None)

    apply_redundant_reads(_conta(job, [_prefixo("vendas")]))

    assert job.redundant_read_bytes_window is None


def test_reading_no_more_than_the_source_is_zero_not_negative():
    """Uma passada só: nada foi relido, e a subtração não pode ficar negativa."""
    job = _job(le=["vendas"], lidos=8 * GB)

    apply_redundant_reads(_conta(job, [_prefixo("vendas", bytes_totais=10 * GB)]))

    assert job.redundant_read_bytes_window == 0.0


def test_several_sources_are_summed_before_subtracting():
    job = _job(le=["vendas", "clientes"], lidos=100 * GB)

    apply_redundant_reads(
        _conta(
            job,
            [
                _prefixo("vendas", bytes_totais=10 * GB),
                _prefixo("clientes", bytes_totais=5 * GB),
            ],
        )
    )

    assert job.redundant_read_bytes_window == 85 * GB


def test_a_job_without_declared_lineage_is_left_alone():
    """Sem saber o que o job lê, não há fonte para comparar."""
    job = _job(le=[])

    assert apply_redundant_reads(_conta(job, [_prefixo("vendas")])) == 0
    assert job.redundant_read_bytes_window is None
