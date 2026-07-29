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

#: Atributos que identificam uma tarifa em cada serviço. O default
#: (`group,operation,usagetype`) serve para Glue e Step Functions e é inútil
#: para S3, onde o que separa Standard de Glacier é `volumeType` dentro de
#: `productFamily`. Sem isto, `inspect` num serviço novo mostra centenas de
#: linhas idênticas e quem confere desiste antes de achar o atributo certo.
_ATRIBUTOS_POR_SERVICO = {
    "AmazonS3": "productFamily,volumeType,storageClass,group",
    "AmazonAthena": "group,operation,usagetype",
    "AWSGlue": "group,operation,usagetype",
    "AWSStepFunctions": "group,operation,usagetype",
}
_ATRIBUTOS_PADRAO = "group,operation,usagetype"


@pricing_app.command("inspect")
def pricing_inspect(
    service: str = typer.Option(
        ..., "--service", help="ServiceCode da Price List (ex.: AWSGlue, AmazonS3)."
    ),
    region: str = typer.Option(
        DEFAULT_REGION, "--region", help="Região cujos preços interessam."
    ),
    sso_profile: str = typer.Option("", "--sso-profile"),
    attributes: str = typer.Option(
        "",
        "--attributes",
        help=(
            "Atributos a exibir, separados por vírgula. Vazio usa os que "
            "identificam tarifa no serviço pedido."
        ),
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

    escolhidos = attributes or _ATRIBUTOS_POR_SERVICO.get(service, _ATRIBUTOS_PADRAO)
    keys = tuple(name.strip() for name in escolhidos.split(",") if name.strip())
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
    only: str = typer.Option(
        "",
        "--only",
        help=(
            "Seções do mapa a atualizar, separadas por vírgula (ex.: s3). "
            "Vazio atualiza todas. As de fora ficam como estão."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve e mostra o resultado sem escrever a tabela.",
    ),
) -> None:
    """Regera a tabela da região. Não escreve nada se alguma tarifa não casar.

    A atomicidade é o que sustenta o `verified = true`: tabela parcial marcada
    como conferida seria pior que a não conferida de hoje. `--only` existe para
    que essa regra não vire bloqueio — um mapeamento novo e ainda não acertado
    não deve impedir a atualização do que já funciona.
    """
    session = make_session(sso_profile or None, region=PRICE_LIST_API_REGION)
    secoes = tuple(part.strip() for part in only.split(",") if part.strip())
    try:
        written, resolutions = refresh_region(
            session.client("pricing"),
            region,
            previous=_previous_pricing,
            sections=secoes,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    falhas = [item for item in resolutions if not item.ok]
    for item in resolutions:
        if item.ok:
            typer.echo(f"  ok    {item.section}.{item.key} = {item.value:g} {item.unit}")
        else:
            label = f"{item.section}.{item.key}" if item.key else item.service
            typer.echo(f"  FALHA {label}: {item.problem}")

    if dry_run:
        resumo = f"{len(resolutions) - len(falhas)} de {len(resolutions)} tarifas casaram"
        typer.echo(f"\n[dry-run] {resumo}; nada foi escrito.")
        if falhas:
            typer.echo(_como_corrigir(falhas, region))
        return

    if written is None:
        raise typer.BadParameter(
            "nenhuma tarifa foi escrita: uma tabela parcial marcada como "
            f"conferida seria pior que a não conferida atual.\n{_como_corrigir(falhas, region)}"
        )
    typer.echo(f"\nEscrito {written}. Revise o diff antes de commitar.")


def _como_corrigir(falhas: list, region: str) -> str:
    """Diz qual `inspect` rodar, em vez de mandar descobrir sozinho."""
    servicos = sorted({item.service for item in falhas if item.service})
    if not servicos:
        return ""
    linhas = ["Para acertar o `match` de knowledge/pricing/mapping.toml:"]
    linhas.extend(
        f"  julius pricing inspect --service {servico} --region {region}"
        for servico in servicos
    )
    secoes = sorted({item.section for item in falhas if item.section})
    if secoes:
        linhas.append(
            "Depois, para testar só o que mudou sem escrever:\n"
            f"  julius pricing refresh --region {region} "
            f"--only {','.join(secoes)} --dry-run"
        )
    return "\n".join(linhas)


def _previous_pricing(region: str):
    """Preserva o que a tabela anterior tinha e o refresh não busca."""
    try:
        return Pricing.for_region(region)
    except UnknownPricingRegionError:
        return None
