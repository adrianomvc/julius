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
    account_name: str = typer.Option(
        "",
        "--account-name",
        help=(
            "Nome da conta no cadastro. Restringe o Glue Catalog aos bancos "
            "terminados nele; vazio usa o --sso-profile."
        ),
    ),
    glue_databases: str = typer.Option(
        "",
        "--glue-databases",
        help="Bancos do catálogo, separados por vírgula. Substitui a regra de nome.",
    ),
    s3_full_listing: bool = typer.Option(
        False,
        "--s3-full-listing",
        help=(
            "Lista cada prefixo S3 até o fim, em paralelo, em vez de parar no "
            "teto de páginas. Necessário para recomendar classe de "
            "armazenamento sobre volume medido; cobra requests de LIST, e o "
            "total gasto aparece na saúde da coleta."
        ),
    ),
    output: str = typer.Option("data/collected/account.json", "--output", "-o"),
) -> None:
    """Coleta em sa-east-1 com o perfil SSO selecionado e grava o dataset."""
    from julius.collection.health.recorder import RequiredCollectionError
    from julius.collection.normalizers.dump import account_to_dataset
    from julius.collection.orchestrator import collect_account
    from julius.collection.scope import CatalogScope

    session = make_session(sso_profile or None, "sa-east-1")
    # O nome da conta cai para o perfil SSO porque no cadastro os dois costumam
    # coincidir (`.julius-accounts.example.json`): quem roda multi-conta não
    # precisa digitar a mesma coisa duas vezes.
    scope = CatalogScope(
        account_name=account_name or sso_profile,
        databases=tuple(
            part.strip() for part in glue_databases.split(",") if part.strip()
        ),
    )
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
            catalog_scope=scope,
            s3_full_listing=s3_full_listing,
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
    for line in slowest_sources(account.collection_health):
        typer.echo(line)
    typer.echo(f"Rode: julius report --input {out}")


def slowest_sources(health: list, limit: int = 5) -> list[str]:
    """As fontes mais lentas da coleta, para quem for otimizá-la.

    O tempo por fonte já era medido e gravado (`CollectionHealth.duration_ms`);
    ele só não era mostrado a ninguém. Sem isto, decidir onde otimizar a coleta
    depende de ler o JSON — e na prática vira palpite sobre qual chamada AWS
    está cara.
    """
    ranked = sorted(
        (entry for entry in health if entry.duration_ms > 0),
        key=lambda entry: entry.duration_ms,
        reverse=True,
    )[:limit]
    if not ranked:
        return []
    total = sum(entry.duration_ms for entry in health)
    lines = [f"Tempo por fonte (total {total / 1000:.1f}s), as mais lentas:"]
    lines.extend(
        f"  {entry.duration_ms / 1000:6.1f}s  {entry.source}"
        + (f" · {entry.collected} itens" if entry.collected else "")
        for entry in ranked
    )
    return lines
