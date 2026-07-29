"""Coleta ao vivo (boto3) → Account, o mesmo modelo do dataset exportado.

O orquestrador não sabe quais fontes existem: ele monta a janela, verifica a
identidade e percorre `SOURCES`. Fonte nova é uma linha de dado em
`collection/sources.py`, não uma inserção no meio desta função.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import boto3

from julius.collection.collectors.last_read import apply_last_read
from julius.collection.health import CollectionRecorder, RequiredCollectionError
from julius.collection.models import Account
from julius.collection.scope import CatalogScope
from julius.collection.session import make_client
from julius.collection.settings import ANALYSIS_WINDOW_DAYS
from julius.collection.sources import SOURCES, CollectionContext, run
from julius.collection.window import AnalysisWindow, BillingMonth


def collect_account(
    session: boto3.Session,
    *,
    config: Any,
    account_id: str | None = None,
    lookback_days: int = ANALYSIS_WINDOW_DAYS,
    touches_table: str = "",
    athena_workgroup: str = "julius",
    athena_output: str | None = None,
    include_cloudtrail: bool = False,
    datawarm_job: str = "",
    catalog_scope: CatalogScope | None = None,
    s3_full_listing: bool = False,
    now: datetime | None = None,
) -> Account:
    """Coleta uma conta. `config` chega de cima e não tem default aqui.

    A camada de coleta não conhece a classe de configuração nem as tabelas de
    domínio que ela carrega: recebe o objeto, lê atributos nomeados e repassa.
    É o que mantém a seta apontando só para baixo.
    """
    health = CollectionRecorder()
    # Duas janelas, construídas uma vez, ambas em UTC. Nenhum coletor volta a
    # decidir sozinho qual período está olhando.
    window = AnalysisWindow.trailing(days=lookback_days, now=now)
    billing = BillingMonth.current(now=now)

    ident = _verified_identity(session, health, account_id)
    account = Account(
        account_id=ident,
        region=session.region_name or "us-east-1",
        period=window.label,
        lookback_days=window.days,
        generated_at=window.end.date().isoformat(),
        window_start=window.start_date.isoformat(),
        window_end=window.data_through.isoformat(),
        window_days=window.days,
    )
    context = CollectionContext(
        session=session,
        window=window,
        billing=billing,
        account=account,
        config=config,
        touches_table=touches_table,
        athena_workgroup=athena_workgroup,
        athena_output=athena_output,
        include_cloudtrail=include_cloudtrail,
        datawarm_job=datawarm_job,
        catalog_scope=catalog_scope or CatalogScope(),
        s3_full_listing=s3_full_listing,
        glue_usage_markers=config.glue_cost.usage_type_markers,
        allocatable_glue_buckets=config.glue_cost.allocatable_buckets,
        glue_cost_version=config.glue_cost.version,
        redshift_usage_markers=config.redshift_cost.usage_type_markers,
        redshift_compute_buckets=config.redshift_cost.compute_buckets,
        redshift_cost_version=config.redshift_cost.version,
    )

    for source in SOURCES:
        run(source, context, health)

    # Derivação pura, depois de tudo coletado: a última leitura de uma tabela
    # sai do histórico de queries do Athena, e é o que liga um prefixo S3 a uma
    # data de **leitura** em vez de só a data da última escrita. Não faz chamada
    # AWS, então não é fonte — mas o alcance dela entra na saúde, porque
    # recomendar classe de armazenamento depende inteiramente dessa cobertura.
    _record_read_evidence(account, health)

    account.collection_health = health.entries
    return account


def _record_read_evidence(account: Account, health: CollectionRecorder) -> None:
    total = len(account.tables)
    if not total:
        return
    medidas = apply_last_read(account)
    if medidas == total:
        return
    health.unavailable(
        "Última leitura de tabelas",
        category="bounded_or_incomplete" if medidas else "no_data",
        impact=(
            f"{total - medidas} de {total} tabela(s) sem data de última leitura: "
            "para elas só se conhece a última escrita, que não diz se o dado é usado"
        ),
        next_action=(
            "configurar --touches-table, ampliar a janela do histórico Athena, "
            "ou habilitar server access logging nos buckets"
        ),
        affects_status=False,
    )


def _verified_identity(
    session: boto3.Session, health: CollectionRecorder, account_id: str | None
) -> str:
    """Sem identidade confirmada não é seguro atribuir o scan a uma conta."""
    actual = health.capture(
        "AWS identity",
        lambda: str(make_client(session, "sts").get_caller_identity()["Account"]),
        "",
        required=True,
        count=lambda value: 1 if value else 0,
        expected=1,
        impact="sem identidade verificada não é seguro atribuir o scan",
        next_action="renovar o login SSO e verificar o Account ID",
    )
    if account_id and account_id != actual:
        health.entries[-1].status = "error"
        health.entries[-1].error_category = "identity_mismatch"
        raise RequiredCollectionError("AWS identity", "identity_mismatch")
    return account_id or actual
