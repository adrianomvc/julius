"""Coleta ao vivo (boto3) → Account, o mesmo modelo do dataset exportado.

O orquestrador não sabe quais fontes existem: ele monta a janela, verifica a
identidade e percorre `SOURCES`. Fonte nova é uma linha de dado em
`collection/sources.py`, não uma inserção no meio desta função.
"""

from __future__ import annotations

from datetime import datetime

import boto3

from julius.collection.health import CollectionRecorder, RequiredCollectionError
from julius.collection.models import Account
from julius.collection.settings import ANALYSIS_WINDOW_DAYS
from julius.collection.sources import SOURCES, CollectionContext, run
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG, Config


def collect_account(
    session: boto3.Session,
    *,
    account_id: str | None = None,
    lookback_days: int = ANALYSIS_WINDOW_DAYS,
    touches_table: str = "",
    athena_workgroup: str = "julius",
    athena_output: str | None = None,
    include_cloudtrail: bool = False,
    datawarm_job: str = "",
    config: Config = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> Account:
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
    )

    for source in SOURCES:
        run(source, context, health)

    account.collection_health = health.entries
    return account


def _verified_identity(
    session: boto3.Session, health: CollectionRecorder, account_id: str | None
) -> str:
    """Sem identidade confirmada não é seguro atribuir o scan a uma conta."""
    actual = health.capture(
        "AWS identity",
        lambda: str(session.client("sts").get_caller_identity()["Account"]),
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
