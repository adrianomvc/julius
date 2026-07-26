"""Coleta ao vivo de uma conta AWS."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from julius.cli._shared import (
    app,
)
from julius.collection.session import make_session
from julius.config import ANALYSIS_WINDOW_DAYS, DEFAULT_CONFIG


@app.command()
def collect(
    sso_profile: str = typer.Option(
        "",
        "--sso-profile",
        help="Nome do perfil SSO no AWS CLI; vazio usa default/AWS_PROFILE.",
    ),
    lookback_days: int = typer.Option(
        ANALYSIS_WINDOW_DAYS,
        "--lookback-days",
        help="Dias UTC completos da janela de análise (custo e comportamento).",
    ),
    touches_table: str = typer.Option("", "--touches-table", help="Tabela oficial de toques (Athena)."),
    athena_workgroup: str = typer.Option("julius", "--athena-workgroup"),
    athena_output: str = typer.Option("", "--athena-output", help="S3 de resultados do Athena."),
    cloudtrail: bool = typer.Option(
        False, "--cloudtrail", help="Coleta evidências de ator no Event history."
    ),
    datawarm_job: str = typer.Option(
        "", "--datawarm-job", help="Nome/identificador do job publicador DataWarm."
    ),
    output: str = typer.Option("data/collected/account.json", "--output", "-o"),
) -> None:
    """Coleta em sa-east-1 com o perfil SSO selecionado e grava o dataset."""
    from julius.collection.health.recorder import RequiredCollectionError
    from julius.collection.normalizers.dump import account_to_dataset
    from julius.collection.orchestrator import collect_account

    session = make_session(sso_profile or None, "sa-east-1")
    try:
        account = collect_account(
            session,
            config=DEFAULT_CONFIG,
            lookback_days=lookback_days,
            touches_table=touches_table,
            athena_workgroup=athena_workgroup,
            athena_output=athena_output or None,
            include_cloudtrail=cloudtrail,
            datawarm_job=datawarm_job,
        )
    except RequiredCollectionError as exc:
        raise typer.BadParameter(str(exc)) from exc

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(account_to_dataset(account), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    typer.echo(
        f"Coletado: conta {account.account_id} · {len(account.glue_jobs)} jobs · "
        f"{len(account.athena_queries)} queries · {len(account.services)} serviços -> {out}"
    )
    typer.echo(
        f"Saúde da coleta: {account.collection_status} · "
        f"{len(account.collection_health)} fontes registradas"
    )
    typer.echo(f"Rode: julius report --input {out}")
