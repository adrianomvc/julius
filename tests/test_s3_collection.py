"""O inventário de S3 chega à coleta ao vivo, e chega por dedução.

As regras de S3 existiam, os modelos existiam, os testes existiam — e os
coletores não estavam em `SOURCES`. As oportunidades de S3 funcionavam sobre
dataset exportado e não funcionariam numa conta real: o `julius collect` nunca
olhava para S3.

O que este arquivo cobra não é só que a fonte roda. É *como* ela decide onde
olhar. Descobrir bucket exigiria `s3:ListAllMyBuckets`, que amplia o alcance da
credencial que o produto pede, e varrer bucket atrás de prefixo cobra por
request — custaria dinheiro para descobrir custo. Então o escopo é derivado do
que outras fontes já coletaram: a location da tabela, o event log do job, a
saída do workgroup.
"""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from julius.collection import sources as collect_module
from julius.collection.collectors import s3
from julius.collection.collectors.glue import jobs
from julius.collection.models import Account, AthenaCoverage, GlueJob, Table
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG


def _conta() -> Account:
    return Account(
        account_id="123456789012",
        tables=[Table(name="db.vendas", location="s3://lake/vendas/")],
        glue_jobs=[
            GlueJob(name="agrega", spark_event_logs_path="s3://logs/spark/agrega/")
        ],
        athena_coverage=AthenaCoverage(
            workgroup_output_locations={"primary": "s3://resultados/athena/"}
        ),
    )


# --------------------------------------------------------------------------
# O escopo é derivado, nunca descoberto
# --------------------------------------------------------------------------


def test_the_scope_comes_from_what_other_sources_already_collected():
    prefixos = s3.known_prefixes(_conta())

    por_tipo = {kind for _location, kind, _asset in prefixos}
    assert por_tipo == {"table_location", "spark_logs", "athena_results", "staging"}
    assert ("s3://lake/vendas/", "table_location", "db.vendas") in prefixos
    assert ("s3://logs/spark/agrega/", "spark_logs", "agrega") in prefixos
    assert ("s3://resultados/athena/", "athena_results", "primary") in prefixos


def test_staging_paths_are_derived_from_the_table_locations():
    """Staging não é declarado em lugar nenhum; sai da location mais o marcador."""
    prefixos = s3.known_prefixes(_conta())

    staging = [loc for loc, kind, _ in prefixos if kind == "staging"]
    assert "s3://lake/vendas/_temporary" in staging
    assert all(caminho.startswith("s3://lake/vendas/") for caminho in staging)


def test_two_tables_on_the_same_location_are_listed_once():
    """Listar o mesmo prefixo duas vezes cobra dois requests pela mesma resposta."""
    conta = Account(
        account_id="1",
        tables=[
            Table(name="db.a", location="s3://lake/comum/"),
            Table(name="db.b", location="s3://lake/comum"),
        ],
    )

    locations = [loc for loc, kind, _ in s3.known_prefixes(conta) if kind == "table_location"]
    assert len(locations) == 1


def test_a_table_without_location_contributes_nothing():
    conta = Account(account_id="1", tables=[Table(name="db.sem_location")])

    assert s3.known_prefixes(conta) == []


def test_bucket_names_come_from_the_prefixes_never_from_listing_buckets():
    nomes = s3.bucket_names(s3.known_prefixes(_conta()))

    assert nomes == ["lake", "logs", "resultados"]


def test_list_buckets_is_not_in_the_allowlist():
    """Se um dia alguém trocar dedução por descoberta, isto falha primeiro."""
    from tests.test_read_only import OPERACOES_PERMITIDAS

    assert "list_buckets" not in OPERACOES_PERMITIDAS
    assert "list_all_my_buckets" not in OPERACOES_PERMITIDAS


# --------------------------------------------------------------------------
# A location da tabela, que faltava
# --------------------------------------------------------------------------


def test_the_catalog_now_carries_the_table_location():
    """Ela já vinha na mesma resposta do GetTables e era descartada."""
    glue = boto3.client(
        "glue",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    stub = Stubber(glue)
    stub.add_response(
        "get_tables",
        {
            "TableList": [
                {
                    "Name": "vendas",
                    "StorageDescriptor": {"Location": "s3://lake/vendas/"},
                    "Parameters": {"Owner": "squad-avi"},
                }
            ]
        },
        {"DatabaseName": "db"},
    )

    with stub:
        tabelas = jobs.collect_tables(glue, ["db"])

    assert tabelas[0].location == "s3://lake/vendas/"
    assert tabelas[0].owner_tag == "squad-avi"


# --------------------------------------------------------------------------
# As fontes, no orquestrador
# --------------------------------------------------------------------------


def _contexto(conta: Account, cliente) -> collect_module.CollectionContext:
    class _Session:
        region_name = "sa-east-1"

        def client(self, _name, **_kwargs):
            return cliente

    return collect_module.CollectionContext(
        session=_Session(),
        window=AnalysisWindow.trailing(),
        billing=BillingMonth.current(),
        account=conta,
        config=DEFAULT_CONFIG,
    )


class _RegistraChamadas:
    def __init__(self):
        self.prefixos: list[str] = []

    def get_paginator(self, _name):
        registro = self.prefixos

        class _Pages:
            def paginate(self, **kwargs):
                registro.append(kwargs.get("Prefix", ""))
                yield {"Contents": []}

        return _Pages()


def test_each_prefix_kind_is_aged_by_its_own_threshold():
    """Resultado de query vence em um dia, event log em trinta.

    Uma chamada só, com um limiar médio, marcaria como velho o que não é e
    deixaria passar o que é.
    """
    conta = _conta()
    ctx = _contexto(conta, _RegistraChamadas())
    limiares: list[int] = []
    original = s3.collect_prefixes

    def _espiao(client, *, known, window, stale_after_days, **extra):
        limiares.append(stale_after_days)
        return original(
            client,
            known=known,
            window=window,
            stale_after_days=stale_after_days,
            **extra,
        )

    s3.collect_prefixes = _espiao
    try:
        collect_module._collect_s3_prefixes(ctx)
    finally:
        s3.collect_prefixes = original

    thresholds = DEFAULT_CONFIG.thresholds
    assert thresholds.s3_athena_results_stale_days in limiares
    assert thresholds.s3_spark_logs_stale_days in limiares
    assert thresholds.s3_staging_stale_days in limiares
    # Limiares diferentes de verdade, não o mesmo número repetido.
    assert len(set(limiares)) > 1


def test_the_scope_is_computed_once_and_shared():
    """Três fontes precisam da mesma lista; derivá-la três vezes é desperdício."""
    ctx = _contexto(_conta(), _RegistraChamadas())

    primeiro = collect_module._s3_scope(ctx)
    segundo = collect_module._s3_scope(ctx)

    assert primeiro is segundo


def test_an_inventory_with_no_s3_disables_the_sources_instead_of_failing():
    """Conta sem nada em S3 não é conta com S3 quebrado."""
    ctx = _contexto(Account(account_id="1"), _RegistraChamadas())
    fontes = [
        fonte
        for fonte in collect_module.SOURCES
        if fonte.name in {"Amazon S3", "S3 Prefixes", "S3 Multipart Uploads"}
    ]

    assert len(fontes) == 3
    for fonte in fontes:
        assert fonte.enabled is not None
        assert fonte.enabled(ctx) is False
        # Desabilitada por ausência de inventário não degrada o scan.
        assert fonte.disabled_affects_status(ctx) is False


def test_the_sources_run_after_what_they_derive_from():
    """O escopo sai de tabelas, jobs e workgroups: a ordem da lista é contrato."""
    nomes = [fonte.name for fonte in collect_module.SOURCES]

    for dependencia in ("Glue Catalog", "Glue Jobs", "Athena Queries"):
        assert nomes.index(dependencia) < nomes.index("Amazon S3")
    # E o rateio de custo depende do inventário de buckets.
    assert nomes.index("Amazon S3") < nomes.index("S3 Cost Explorer")
