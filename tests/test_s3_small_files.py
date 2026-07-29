"""Arquivo pequeno nas tabelas que o Athena não enxerga.

A análise já existia e chegava a uma tabela por um caminho só: o
`ATHENA-SMALL-FILES`, cuja evidência vem de `enrich_catalog`, que percorre
`AthenaQuery.reads_tables`. Tabela escrita por Glue e lida por Spark ou EMR não
aparece em `GetQueryExecution` nenhum — e no Data Mesh isso é a maioria.

A evidência para avaliá-las já era coletada: `CatalogScope` seleciona os bancos
da conta, `collect_tables` traz a `location` de cada tabela e `known_prefixes`
vira um prefixo `table_location` que a coleta de S3 lista. Faltava a regra.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from julius.collection.models import (
    Account,
    AthenaQuery,
    S3CostCoverage,
    S3Prefix,
)
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.s3 import small_files

_MB = 1024**2
_GB = 1024**3


def _prefixo(**overrides) -> S3Prefix:
    base = {
        "bucket": "lake",
        "prefix": "compartilhado/vendas/",
        "kind": "table_location",
        "source_asset": "database_db_compartilhado_consumer_avi.vendas",
        "object_count": 8000,
        "total_bytes": 40 * _GB,
        "average_object_bytes": 5 * _MB,
    }
    return S3Prefix(**{**base, **overrides})


def _conta(*, prefixos=None, com_cobranca: bool = True, **overrides) -> Account:
    return Account(
        account_id="123456789012",
        s3_prefixes=prefixos if prefixos is not None else [_prefixo()],
        s3_cost_coverage=(
            S3CostCoverage(
                net_cost=120.0,
                cost_quality="allocated",
                buckets={"storage_standard": 100.0, "requests_read": 20.0},
            )
            if com_cobranca
            else None
        ),
        **overrides,
    )


# ---------------------------------------------------------------------------
# O limiar precisa ser um só
# ---------------------------------------------------------------------------


def test_the_two_small_file_thresholds_agree():
    """O mesmo veredito não pode depender de por qual caminho a tabela chegou.

    O caminho do Athena decide na coleta (`object_evidence` devolve um booleano);
    o do S3 decide na regra. As duas camadas não podem compartilhar a constante —
    `collection` não importa `knowledge` —, então o que resta é este teste.
    """
    from julius.collection.collectors.s3_evidence import (
        SMALL_FILE_MIN_COUNT,
        SMALL_FILE_THRESHOLD_BYTES,
    )

    limiares = DEFAULT_CONFIG.thresholds
    assert limiares.s3_small_file_max_bytes == SMALL_FILE_THRESHOLD_BYTES
    assert limiares.s3_small_files_min_count == SMALL_FILE_MIN_COUNT


# ---------------------------------------------------------------------------
# Quem dispara
# ---------------------------------------------------------------------------


def test_a_shared_database_table_in_small_files_is_found():
    achados = small_files.detect(_conta(), DEFAULT_CONFIG, "scan")

    assert [a.rule_id for a in achados] == ["S3-SMALL-FILES"]
    assert achados[0].asset_name == "s3://lake/compartilhado/vendas/"
    assert "database_db_compartilhado_consumer_avi.vendas" in achados[0].finding


def test_a_workspace_db_table_is_found_the_same_way():
    conta = _conta(
        prefixos=[
            _prefixo(prefix="workspace/experimento/", source_asset="workspace_db.experimento")
        ]
    )

    achados = small_files.detect(conta, DEFAULT_CONFIG, "scan")

    assert len(achados) == 1
    assert "workspace_db.experimento" in achados[0].finding


@pytest.mark.parametrize(
    "override",
    [
        # Arquivo grande: não é o problema.
        {"average_object_bytes": 200 * _MB},
        # Poucos objetos: compactar dez arquivos não paga validar consumidores.
        {"object_count": 20},
        # Não listado: `None` é "não medido", e não se afirma sobre isso.
        {"average_object_bytes": None},
        {"object_count": None},
    ],
)
def test_what_does_not_deserve_the_finding(override):
    assert small_files.detect(_conta(prefixos=[_prefixo(**override)]), DEFAULT_CONFIG, "scan") == []


@pytest.mark.parametrize("kind", ["athena_results", "spark_logs", "staging"])
def test_only_table_prefixes_are_evaluated(kind):
    """Resultado de query e event log são para apagar, não para compactar."""
    conta = _conta(prefixos=[_prefixo(kind=kind)])

    assert small_files.detect(conta, DEFAULT_CONFIG, "scan") == []


# ---------------------------------------------------------------------------
# Sem contar o mesmo trabalho duas vezes
# ---------------------------------------------------------------------------


def test_a_table_the_athena_rule_already_denounces_is_not_repeated():
    """Duas âncoras para o mesmo trabalho enganam quem lê o ranking."""
    conta = _conta()
    conta.athena_queries = [
        AthenaQuery(
            query_id="q1",
            reads_tables=["database_db_compartilhado_consumer_avi.vendas"],
            small_files_confirmed=True,
        )
    ]

    assert small_files.detect(conta, DEFAULT_CONFIG, "scan") == []


def test_a_query_that_reads_the_table_without_confirming_does_not_suppress():
    """Só suprime quem de fato já denunciou; ler a tabela não é denunciar."""
    conta = _conta()
    conta.athena_queries = [
        AthenaQuery(
            query_id="q1",
            reads_tables=["database_db_compartilhado_consumer_avi.vendas"],
            small_files_confirmed=False,
        )
    ]

    assert len(small_files.detect(conta, DEFAULT_CONFIG, "scan")) == 1


def test_the_match_ignores_case_like_the_catalog():
    conta = _conta()
    conta.athena_queries = [
        AthenaQuery(
            query_id="q1",
            reads_tables=["DATABASE_DB_COMPARTILHADO_CONSUMER_AVI.Vendas"],
            small_files_confirmed=True,
        )
    ]

    assert small_files.detect(conta, DEFAULT_CONFIG, "scan") == []


# ---------------------------------------------------------------------------
# A conta: compactar mexe em request, não em armazenamento
# ---------------------------------------------------------------------------


def test_the_baseline_is_the_request_share_not_the_storage():
    """Reivindicar armazenamento prometeria o que a compactação não entrega."""
    achado = small_files.detect(_conta(), DEFAULT_CONFIG, "scan")[0]

    # 20.0 de requests_read rateados sobre 8000 objetos = 0.0025/objeto.
    assert achado.estimation.baseline_cost == pytest.approx(20.0, abs=0.01)
    assert any("request" in item for item in achado.estimation.assumptions)
    assert any(
        "armazenamento permanece" in item for item in achado.estimation.assumptions
    )


def test_the_projection_uses_the_object_count_after_compaction():
    achado = small_files.detect(_conta(), DEFAULT_CONFIG, "scan")[0]

    # 40 GB em blocos de 128 MiB = 320 objetos, de 8000.
    assert "~320" in " ".join(achado.estimation.assumptions)
    assert achado.estimation.projected_cost < achado.estimation.baseline_cost


def test_the_gain_is_strategic_because_nobody_measured_the_reads():
    """Quanto o request cai depende de quantas leituras acontecem."""
    achado = small_files.detect(_conta(), DEFAULT_CONFIG, "scan")[0]

    assert achado.estimation.is_strategic is True
    assert any("leituras desta tabela" in item for item in achado.missing_evidence)


def test_without_classified_request_billing_there_is_no_figure():
    achado = small_files.detect(_conta(com_cobranca=False), DEFAULT_CONFIG, "scan")[0]

    assert achado.blocked is True
    assert achado.estimation.saving_quality == "unavailable"
    assert achado.estimation.estimated_saving == 0.0


def test_storage_only_billing_does_not_become_a_request_saving():
    """Fatura sem linha de request não autoriza afirmar economia de request."""
    conta = _conta()
    conta.s3_cost_coverage = S3CostCoverage(
        net_cost=100.0, buckets={"storage_standard": 100.0}
    )

    achado = small_files.detect(conta, DEFAULT_CONFIG, "scan")[0]

    assert achado.blocked is True


# ---------------------------------------------------------------------------
# O Julius recomenda; quem reescreve é o time dono
# ---------------------------------------------------------------------------


def test_the_recommendation_says_who_rewrites():
    achado = small_files.detect(_conta(), DEFAULT_CONFIG, "scan")[0]

    assert "não reescreve" in achado.how_to_apply
    assert any("aprovação separada" in risco for risco in achado.risks)


def test_a_truncated_listing_says_the_count_is_a_floor():
    conta = _conta(prefixos=[_prefixo(listing_complete=False)])

    achado = small_files.detect(conta, DEFAULT_CONFIG, "scan")[0]

    assert any("piso" in item for item in achado.missing_evidence)
    assert any("parcial" in risco for risco in achado.risks)


def test_the_compaction_target_is_configurable():
    conta = _conta()
    limiares = replace(DEFAULT_CONFIG.thresholds, s3_compaction_target_bytes=256 * _MB)
    config = replace(DEFAULT_CONFIG, thresholds=limiares)

    achado = small_files.detect(conta, config, "scan")[0]

    assert "256 MiB" in achado.recommended_action
    # 40 GB em blocos de 256 MiB = 160 objetos.
    assert "~160" in " ".join(achado.estimation.assumptions)
