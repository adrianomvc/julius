"""Tabela de preço por região: inspeção e regeneração."""

from __future__ import annotations

import typer

from julius.cli._shared import (
    pricing_app,
)
from julius.collection.collectors.pricing import (
    API_REGION as PRICE_LIST_API_REGION,
)
from julius.collection.collectors.pricing import (
    distinct_attributes,
    fetch_products,
)
from julius.collection.session import make_session
from julius.knowledge.pricing import (
    DEFAULT_REGION,
    Pricing,
    UnknownPricingRegionError,
)
from julius.knowledge.pricing.refresh import refresh_region


@pricing_app.command("inspect")
def pricing_inspect(
    service: str = typer.Option(
        ..., "--service", help="ServiceCode da Price List (ex.: AWSGlue, AmazonAthena)."
    ),
    region: str = typer.Option(
        DEFAULT_REGION, "--region", help="Região cujos preços interessam."
    ),
    sso_profile: str = typer.Option("", "--sso-profile"),
    attributes: str = typer.Option(
        "group,operation,usagetype",
        "--attributes",
        help="Atributos a exibir, separados por vírgula.",
    ),
) -> None:
    """Mostra o que a API devolve, para o mapa ser conferido por uma pessoa.

    É o passo que não dá para pular: quais atributos identificam uma tarifa
    varia por serviço e muda com o tempo. Chutar isso dentro do código produz
    número errado com cara de verificado.
    """
    session = make_session(sso_profile or None, region=PRICE_LIST_API_REGION)
    items, problems = fetch_products(session.client("pricing"), service, region)
    for problem in problems:
        typer.echo(f"  ! {problem}")
    if not items:
        raise typer.BadParameter(
            f"nenhum preço on-demand para {service} em {region}."
        )

    keys = tuple(name.strip() for name in attributes.split(",") if name.strip())
    rows = distinct_attributes(items, keys)
    typer.echo(f"{len(items)} dimensões de preço · {len(rows)} combinações\n")
    for row in sorted(rows, key=lambda item: tuple(item.get(k, "") for k in keys)):
        described = " · ".join(f"{key}={row.get(key, '')}" for key in keys)
        prices = ", ".join(f"{value:g}" for value in row["prices"][:4])
        typer.echo(f"  {described}  →  {prices} {row['unit']}")


@pricing_app.command("refresh")
def pricing_refresh(
    region: str = typer.Option(DEFAULT_REGION, "--region"),
    sso_profile: str = typer.Option("", "--sso-profile"),
) -> None:
    """Regera a tabela da região. Não escreve nada se alguma tarifa não casar."""
    session = make_session(sso_profile or None, region=PRICE_LIST_API_REGION)
    written, resolutions = refresh_region(
        session.client("pricing"),
        region,
        previous=_previous_pricing,
    )
    for item in resolutions:
        if item.ok:
            typer.echo(f"  ok    {item.section}.{item.key} = {item.value:g} {item.unit}")
        else:
            label = f"{item.section}.{item.key}" if item.key else item.service
            typer.echo(f"  FALHA {label}: {item.problem}")

    if written is None:
        raise typer.BadParameter(
            "nenhuma tarifa foi escrita: uma tabela parcial marcada como "
            "conferida seria pior que a não conferida atual. Rode "
            f"`julius pricing inspect --service <código> --region {region}` e "
            "ajuste knowledge/pricing/mapping.toml."
        )
    typer.echo(f"\nEscrito {written}. Revise o diff antes de commitar.")


def _previous_pricing(region: str):
    """Preserva o que a tabela anterior tinha e o refresh não busca."""
    try:
        return Pricing.for_region(region)
    except UnknownPricingRegionError:
        return None
