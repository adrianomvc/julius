"""Custo por GET quando o Cost Explorer não reconcilia.

A economia de compactação exige duas coisas: quanto custa um GET e quantos GETs
cada prefixo recebeu. A segunda não tem substituto — métrica de request do S3 é
por bucket e paga, então sem Server Access Logs não há contagem por prefixo.

A primeira tinha substituto pronto e sem uso: a tarifa de GET da tabela
versionada, que a regra de classe de armazenamento já consome. Faltar o rateio
reconciliado bloqueava um achado que já é estratégico — ou seja, que nem entra no
portfólio —, então trocar `unavailable` por um número modelado dá grandeza ao
time sem mexer em nenhum total.
"""

from __future__ import annotations

from dataclasses import replace

from julius.collection.models import Account, S3CostCoverage, S3Prefix
from julius.collection.models.s3 import S3CostLine
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.s3.request_cost import request_estimation
from tests.verified_pricing import verified_config

#: Tarifa de GET conferida, para o teste medir a fórmula e não a tabela.
COM_TARIFA = replace(
    verified_config("s3"),
    pricing=replace(
        verified_config("s3").pricing,
        s3_request_per_1000={"get": 0.44, "list": 5.5},
    ),
)


def _prefixo(nome: str = "s3://lake/vendas/") -> S3Prefix:
    return S3Prefix(
        bucket="lake",
        prefix="vendas/",
        kind="table_location",
        source_asset="vendas",
        object_count=100_000,
        total_bytes=50 * 1024**3,
        get_requests_window=200_000,
        access_quality="best_effort",
        listing_complete=True,
    )


def _conta(coverage: S3CostCoverage | None = None) -> Account:
    return Account(account_id="123456789012", s3_cost_coverage=coverage)


def test_without_cost_explorer_the_versioned_rate_carries_the_estimate():
    """Antes isto era `unavailable` mesmo com a tarifa disponível na tabela."""
    est = request_estimation(
        _conta(), [_prefixo()], COM_TARIFA, method="s3_small_files_requests_v1"
    )

    assert est.saving_quality == "modeled_evidence"
    assert est.baseline_quality == "modeled"
    assert est.baseline_cost > 0
    assert est.pricing_dependencies == ("s3",)
    assert any("tarifa versionada de GET" in a for a in est.assumptions)


def test_cost_explorer_still_wins_when_it_reconciles():
    """A fatura continua sendo a melhor âncora; a tabela é o segundo melhor."""
    coverage = S3CostCoverage(
        buckets={"requests_read": 100.0},
        lines=[
            S3CostLine(
                usage_type="Requests-Tier2",
                bucket="requests_read",
                cost=100.0,
                usage_quantity=1_000_000.0,
                usage_unit="Requests",
            )
        ],
    )

    est = request_estimation(
        _conta(coverage),
        [_prefixo()],
        COM_TARIFA,
        method="s3_small_files_requests_v1",
    )

    assert est.baseline_quality == "allocated"
    assert est.pricing_dependencies == ()
    assert any("UsageQuantity de Requests-Tier2" in a for a in est.assumptions)


def test_without_a_rate_anywhere_the_reason_names_both_sources():
    """Tabela sem tarifa de GET é o estado de quem nunca rodou o refresh."""
    est = request_estimation(
        _conta(), [_prefixo()], DEFAULT_CONFIG, method="s3_small_files_requests_v1"
    )

    assert est.saving_quality == "unavailable"
    assert any(
        "Cost Explorer" in a and "tabela versionada" in a for a in est.assumptions
    )


def test_the_get_count_per_prefix_has_no_substitute():
    """Sem access logs não há contagem por prefixo, e a tarifa não resolve isso.

    Métrica de request do S3 é por bucket e paga; nada no que o Julius já coleta
    diz quantos GETs um prefixo recebeu.
    """
    sem_logs = replace(_prefixo(), get_requests_window=None, access_quality="unavailable")

    est = request_estimation(
        _conta(), [sem_logs], COM_TARIFA, method="s3_small_files_requests_v1"
    )

    assert est.saving_quality == "unavailable"
    assert any("access logs" in a for a in est.assumptions)


def test_the_finding_stays_out_of_the_portfolio_either_way():
    """Destravar a grandeza não pode virar economia somável."""
    modelada = request_estimation(
        _conta(), [_prefixo()], COM_TARIFA, method="s3_small_files_requests_v1"
    )

    assert modelada.is_strategic is True
