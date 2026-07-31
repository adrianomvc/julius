"""Fonte inferida destrava cobertura e acionabilidade, nunca a cifra.

É a regra que sustenta o valor do portfólio inteiro: cifra precisa de
procedência. Uma fonte que **mede** pode sustentar economia; uma que **infere**
melhora cobertura, resolve responsável e alimenta investigação — e para por aí.

Sem este invariante travado, cada frente nova de evidência substituta decide
sozinha o quanto pode afirmar, e a regra volta a ser convenção em vez de
garantia. É o análogo do invariante que já existe para dependência de pricing.
"""

from __future__ import annotations

from julius.collection.collectors.last_read import apply_last_read
from julius.collection.models import Account, GlueJob, S3Prefix, Table
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.s3.storage_class import _dias_sem_leitura

ONTEM = "2026-05-01T03:00:00+00:00"

#: Qualidades produzidas por inferência, e não por medição. Cada uma vem de uma
#: fonte substituta diferente; nenhuma pode sustentar cifra.
QUALIDADES_INFERIDAS = ("process_inferred",)


def _conta_com_leitura_por_linhagem() -> Account:
    conta = Account(
        account_id="123456789012",
        # Janela profunda: `_apply_prefix_read_evidence` copia `window_days` para
        # `read_coverage_days`, e com 30 dias o gate de cobertura devolveria
        # `None` antes de o gate de qualidade ser consultado — o teste passaria
        # sem exercitar o que afirma testar.
        window_days=180,
        window_end="2026-07-31",
        tables=[Table(name="vendas", location="s3://lake/vendas/")],
        s3_prefixes=[
            S3Prefix(
                bucket="lake",
                prefix="vendas/",
                kind="table_location",
                source_asset="vendas",
                object_count=100,
                total_bytes=1024,
            )
        ],
        glue_jobs=[
            GlueJob(name="agrega", reads_tables=["vendas"], last_run_at=ONTEM)
        ],
    )
    apply_last_read(conta)
    return conta


def test_a_lineage_inferred_read_does_not_produce_a_cold_age():
    """A data prova que o dado é consumido, não quando o prefixo foi lido.

    Um job pode ler só a partição do dia. Concluir "frio há N dias" a partir
    dela transformaria inferência em cifra.
    """
    conta = _conta_com_leitura_por_linhagem()
    prefixo = conta.s3_prefixes[0]

    dias, _fonte, qualidade = _dias_sem_leitura(
        prefixo, {}, None, conta, DEFAULT_CONFIG
    )

    assert prefixo.access_quality == "process_inferred"
    assert dias is None, "inferência não pode virar idade de dado frio"
    assert qualidade == "process_inferred"


def test_every_inferred_quality_is_rejected_by_the_storage_class_gate():
    """Vale para toda qualidade inferida, não só a que existe hoje."""
    conta = _conta_com_leitura_por_linhagem()
    prefixo = conta.s3_prefixes[0]

    for qualidade in QUALIDADES_INFERIDAS:
        prefixo.access_quality = qualidade
        dias, _fonte, saida = _dias_sem_leitura(
            prefixo, {}, None, conta, DEFAULT_CONFIG
        )
        assert dias is None, f"{qualidade} sustentou cifra"
        assert saida == qualidade


def test_the_measured_path_still_produces_a_figure():
    """O invariante não pode ser cumprido bloqueando tudo."""
    conta = _conta_com_leitura_por_linhagem()
    prefixo = conta.s3_prefixes[0]
    prefixo.access_quality = "prefix_inferred"  # histórico de query observado

    dias, _fonte, qualidade = _dias_sem_leitura(
        prefixo, {}, None, conta, DEFAULT_CONFIG
    )

    assert dias is not None
    assert qualidade == "prefix_inferred"


def test_the_inference_still_improves_coverage():
    """O ganho existe e é este: a tabela deixa de estar sem evidência nenhuma."""
    conta = _conta_com_leitura_por_linhagem()

    assert conta.tables[0].last_read_at == ONTEM
    assert conta.tables[0].last_read_source == "process_lineage"
