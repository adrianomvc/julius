"""Gera a tabela de preço de uma região a partir da Price List API.

Deliberadamente **não** roda durante o scan. Consultar preço a cada análise
acrescentaria latência e uma permissão a toda execução, e tiraria a
reprodutibilidade: dois scans do mesmo dataset poderiam divergir porque a AWS
mudou uma tarifa no meio. Aqui o preço vira um arquivo que alguém revisa e
commita — a mudança fica num diff, com data.

Regra que sustenta o `verified`: se qualquer tarifa do mapa não casar com
exatamente um preço, **nada é escrito**. Tabela parcial marcada como conferida
seria pior que a tabela não conferida de hoje.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from julius.collection.collectors.pricing import PriceItem, fetch_products

MAPPING = Path(__file__).resolve().parent / "mapping.toml"
TABLES = Path(__file__).resolve().parent / "tables"


@dataclass(frozen=True)
class Resolution:
    """O que aconteceu ao tentar resolver uma tarifa."""

    section: str
    key: str
    service: str
    value: float | None = None
    # Uma entrada com `expand` rende uma tarifa por valor de atributo, não um
    # escalar: é assim que instância de SageMaker entra no mapa sem exigir um
    # bloco por tipo.
    rates: tuple[tuple[str, float], ...] = ()
    unit: str = ""
    candidates: int = 0
    problem: str = ""
    effective_date: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None or bool(self.rates)


def load_mapping(path: Path = MAPPING) -> dict[str, dict[str, dict]]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def resolve(
    mapping: dict[str, dict[str, dict]],
    products: dict[str, list[PriceItem]],
) -> list[Resolution]:
    """Aplica o mapa aos itens trazidos da API, uma tarifa por vez."""
    out: list[Resolution] = []
    for section, entries in mapping.items():
        for key, spec in entries.items():
            service = str(spec.get("service", ""))
            criteria = {str(k): str(v) for k, v in (spec.get("match") or {}).items()}
            pick = str(spec.get("pick", "only"))
            matches = [
                item for item in products.get(service, []) if item.matches(criteria)
            ]
            expand = str(spec.get("expand", ""))
            if expand:
                out.append(
                    _expand(section, key, service, matches, criteria, pick, expand)
                )
                continue
            prices = sorted({item.usd_per_unit for item in matches})
            if not prices:
                out.append(
                    Resolution(
                        section, key, service,
                        problem=f"nenhum preço casou com {criteria}",
                    )
                )
                continue
            if pick == "only" and len(prices) > 1:
                out.append(
                    Resolution(
                        section, key, service, candidates=len(prices),
                        problem=(
                            f"{len(prices)} preços distintos casaram ({prices[:4]}); "
                            "restrinja o `match` ou use pick = \"min\"/\"max\""
                        ),
                    )
                )
                continue
            value = {"min": prices[0], "max": prices[-1]}.get(pick, prices[0])
            out.append(
                Resolution(
                    section, key, service,
                    value=value,
                    unit=matches[0].unit,
                    candidates=len(prices),
                    effective_date=matches[0].effective_date,
                )
            )
    return out


def _expand(
    section: str,
    key: str,
    service: str,
    matches: list[PriceItem],
    criteria: dict[str, str],
    pick: str,
    attribute: str,
) -> Resolution:
    """Uma tarifa por valor de `attribute`, em vez de um escalar.

    Tarifa de instância de SageMaker ficou fora do mapa porque um bloco por tipo
    de instância é longo demais para alguém conferir. Mas a decisão humana ali é
    uma só — *quais atributos identificam a hora de instância daquele
    componente* — e ela não muda com o tipo. `expand` mantém essa decisão
    explícita e deixa a lista de tipos vir da API: um tipo novo na conta ganha
    tarifa sem editar o mapa, em vez de bloquear a cifra por omissão.

    A atomicidade do refresh não é relaxada: ambiguidade em qualquer valor
    reprova a entrada inteira, e uma entrada reprovada impede a escrita.
    """
    grouped: dict[str, list[float]] = {}
    for item in matches:
        value = item.attributes.get(attribute, "")
        if value:
            grouped.setdefault(value, []).append(item.usd_per_unit)
    if not grouped:
        return Resolution(
            section,
            key,
            service,
            problem=(
                f"nenhum preço com atributo {attribute} casou com {criteria}"
            ),
        )
    rates: dict[str, float] = {}
    for value, prices in grouped.items():
        distinct = sorted(set(prices))
        if pick == "only" and len(distinct) > 1:
            return Resolution(
                section,
                key,
                service,
                candidates=len(distinct),
                problem=(
                    f"{len(distinct)} preços distintos para {attribute}={value} "
                    f"({distinct[:4]}); restrinja o `match` ou use "
                    'pick = "min"/"max"'
                ),
            )
        rates[value] = {"min": distinct[0], "max": distinct[-1]}.get(
            pick, distinct[0]
        )
    return Resolution(
        section,
        key,
        service,
        rates=tuple(sorted(rates.items())),
        unit=matches[0].unit,
        candidates=len(rates),
        effective_date=matches[0].effective_date,
    )


def carried_sections(
    region: str, refreshed: set[str], *, tables: Path = TABLES
) -> dict[str, dict[str, float]]:
    """Seções da tabela atual que este refresh não vai buscar.

    Sem isto, `--only s3` escreveria uma tabela contendo apenas S3 e apagaria
    Glue, Athena e Step Functions — tarifas conferidas antes, perdidas por uma
    execução que nem as consultou.
    """
    path = tables / f"{region}.toml"
    if not path.is_file():
        return {}
    tabela = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        nome: {
            chave: float(valor)
            for chave, valor in conteudo.items()
            if isinstance(valor, (int, float))
        }
        for nome, conteudo in tabela.items()
        # `sagemaker` tem tratamento próprio (instâncias aninhadas) e escalares
        # de topo não são seção.
        if isinstance(conteudo, dict) and nome not in refreshed and nome != "sagemaker"
    }


def carried_verification(
    region: str, *, tables: Path = TABLES
) -> dict[str, dict[str, str | bool]]:
    """Metadados de conferência permanecem ligados à seção que foi verificada."""
    path = tables / f"{region}.toml"
    if not path.is_file():
        return {}
    table = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        str(section): {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, bool))
        }
        for section, metadata in (table.get("verification") or {}).items()
        if isinstance(metadata, dict)
    }


def render_table(
    region: str,
    resolutions: list[Resolution],
    *,
    today: date,
    sagemaker: dict[str, float] | None = None,
    sagemaker_components: dict[str, dict[str, float]] | None = None,
    sagemaker_default: float,
    carry: dict[str, dict[str, float]] | None = None,
    verification: dict[str, dict[str, str | bool]] | None = None,
    refreshed_sections: set[str] | None = None,
) -> str:
    """Escreve o TOML da região, já marcado como conferido."""
    by_section: dict[str, dict[str, float]] = {
        nome: dict(valores) for nome, valores in (carry or {}).items()
    }
    for item in resolutions:
        if item.value is not None:
            by_section.setdefault(item.section, {})[item.key] = item.value

    # Componente de SageMaker é tabela aninhada, não escalar de seção: o que o
    # mapa resolveu substitui o que a tabela anterior trazia, para uma tarifa
    # que a API deixou de listar não sobreviver dentro de uma seção agora
    # marcada como conferida.
    components: dict[str, dict[str, float]] = {
        name: dict(rates) for name, rates in (sagemaker_components or {}).items()
    }
    for item in resolutions:
        if item.section == "sagemaker" and item.rates:
            components[item.key] = dict(item.rates)
    sagemaker_scalars = by_section.pop("sagemaker", {})
    default_hourly = sagemaker_scalars.pop("default_hourly", sagemaker_default)

    provider_dates = sorted(
        item.effective_date[:10]
        for item in resolutions
        if item.effective_date
    )
    provider_effective = provider_dates[0] if provider_dates else ""
    lines = [
        "# Gerado por `julius pricing refresh` a partir da Price List API.",
        "# Revise o diff antes de commitar: mudança de tarifa muda toda",
        "# estimativa modelada da conta.",
        "",
        f'region = "{region}"',
        'currency = "USD"',
        f'version = "pricelist-{today.isoformat()}"',
        "verified = true",
        f'effective_date = "{provider_effective}"',
        f'verified_at = "{today.isoformat()}"',
        "sources = [",
        '    "https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/'
        'using-price-list-query-api.html",',
        "]",
    ]
    section_verification = {
        name: dict(metadata) for name, metadata in (verification or {}).items()
    }
    for section in refreshed_sections or set(by_section):
        section_verification[section] = {
            "verified": True,
            "verified_at": today.isoformat(),
        }
    for section, metadata in sorted(section_verification.items()):
        lines.extend(["", f"[verification.{section}]"])
        lines.append(
            f"verified = {'true' if bool(metadata.get('verified')) else 'false'}"
        )
        lines.append(f'verified_at = "{metadata.get("verified_at", "")}"')
    # As seções saem do que o mapa resolveu, não de uma lista escrita aqui: uma
    # lista fixa faria a seção nova do `mapping.toml` resolver, casar e ser
    # descartada na hora de escrever — sem erro, sem aviso, sem tarifa.
    for section, values in sorted(by_section.items()):
        if not values:
            continue
        lines.extend(["", f"[{section}]"])
        lines.extend(f"{key} = {value!r}" for key, value in sorted(values.items()))

    lines.extend(["", "[sagemaker]", f"default_hourly = {default_hourly!r}"])
    lines.extend(
        f"{key} = {value!r}" for key, value in sorted(sagemaker_scalars.items())
    )
    if sagemaker:
        lines.extend(["", "[sagemaker.instances]"])
        lines.extend(
            f'"{name}" = {value!r}' for name, value in sorted(sagemaker.items())
        )
    for component, rates in sorted(components.items()):
        if not rates:
            continue
        lines.extend(["", f"[sagemaker.components.{component}]"])
        lines.extend(
            f'"{name}" = {value!r}' for name, value in sorted(rates.items())
        )
    return "\n".join(lines) + "\n"


def refresh_region(
    pricing_client,
    region: str,
    *,
    today: date | None = None,
    tables: Path = TABLES,
    mapping_path: Path = MAPPING,
    previous: Callable[[str], Any] | None = None,
    sections: tuple[str, ...] = (),
    dry_run: bool = False,
) -> tuple[Path | None, list[Resolution]]:
    """Consulta, resolve e escreve. Devolve `(caminho ou None, resoluções)`.

    O caminho vem `None` quando alguma tarifa não resolveu — e nesse caso o
    arquivo existente não é tocado. A regra é deliberada: uma tabela parcial
    marcada como `verified = true` seria pior que a não conferida de hoje.

    `sections` restringe o refresh a parte do mapa. Existe porque a atomicidade
    acima, sem ele, transforma um mapeamento novo e ainda não conferido num
    bloqueio para tudo que já funcionava: acrescentar nove entradas de S3
    impediria de atualizar Glue e Athena até que as nove casassem. As seções de
    fora são preservadas como estão, não apagadas.

    `dry_run` resolve e relata sem escrever — é o laço de conferência que o
    `inspect` sozinho não fecha.
    """
    today = today or date.today()
    mapping = load_mapping(mapping_path)
    if sections:
        desconhecidas = sorted(set(sections) - set(mapping))
        if desconhecidas:
            raise ValueError(
                f"seções inexistentes no mapa: {', '.join(desconhecidas)}; "
                f"disponíveis: {', '.join(sorted(mapping))}"
            )
        mapping = {nome: entradas for nome, entradas in mapping.items() if nome in sections}
    services = {
        str(spec.get("service", ""))
        for entries in mapping.values()
        for spec in entries.values()
    }
    products: dict[str, list[PriceItem]] = {}
    problems: list[Resolution] = []
    for service in sorted(services):
        items, issues = fetch_products(pricing_client, service, region)
        products[service] = items
        problems.extend(
            Resolution("", "", service, problem=issue) for issue in issues
        )

    resolutions = resolve(mapping, products)
    if problems and not any(item.ok for item in resolutions):
        return None, problems + resolutions
    if not all(item.ok for item in resolutions):
        return None, resolutions
    if dry_run:
        return None, resolutions

    # `[sagemaker.instances]` é a lista escrita à mão de antes do mapa. Quando
    # este refresh atualiza a seção SageMaker, ela é descartada: carregá-la para
    # dentro de uma seção que acabou de ser marcada como conferida daria
    # procedência de Price List a uma tarifa que ninguém conferiu. Sem tarifa, a
    # regra bloqueia a cifra — que é o comportamento desejado.
    keep = previous(region) if previous else None
    text = render_table(
        region,
        resolutions,
        today=today,
        sagemaker=(
            None
            if "sagemaker" in mapping
            else getattr(keep, "sagemaker_instances", None)
        ),
        sagemaker_components=getattr(
            keep, "sagemaker_component_instances", None
        ),
        sagemaker_default=float(getattr(keep, "sagemaker_default_hourly", 0.18)),
        carry=carried_sections(region, set(mapping), tables=tables),
        verification=carried_verification(region, tables=tables),
        refreshed_sections=set(mapping),
    )
    target = tables / f"{region}.toml"
    target.write_text(text, encoding="utf-8")
    return target, resolutions
