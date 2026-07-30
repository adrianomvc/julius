"""Leitura da Price List API da AWS.

Este módulo **não sabe** qual combinação de atributos identifica "Glue DPU-hora
STANDARD em São Paulo". Ele traz os itens de preço já normalizados e deixa a
identificação para um mapa declarado em `knowledge/pricing/mapping.toml`.

A razão é honesta: os atributos variam por serviço e mudam com o tempo, e
chutá-los dentro do código produziria uma tarifa errada com cara de verificada.
Com o mapa, `julius pricing inspect` mostra o que a API realmente devolve para
uma região, alguém confere uma vez, e `julius pricing refresh` passa a
reproduzir aquilo — falhando alto se algum item deixar de casar.

A Price List API só existe em algumas regiões (us-east-1 entre elas) e responde
sobre todas as outras. A região consultada é sempre um filtro, nunca o endpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Endpoint da própria API — não é a região cujos preços queremos.
API_REGION = "us-east-1"


@dataclass(frozen=True)
class PriceItem:
    """Um preço on-demand, já extraído do JSON aninhado da API."""

    sku: str
    service_code: str
    attributes: dict[str, str]
    usd_per_unit: float
    unit: str
    description: str
    effective_date: str = ""

    def matches(self, criteria: dict[str, str]) -> bool:
        return all(
            self.attributes.get(key, "") == value for key, value in criteria.items()
        )


def fetch_products(
    pricing_client, service_code: str, region: str, *, max_pages: int = 20
) -> tuple[list[PriceItem], list[str]]:
    """Itens de preço on-demand de um serviço numa região, e o que falhou.

    O filtro por região usa `regionCode`; contas ou versões que ainda respondem
    apenas por `location` caem no segundo filtro. Nenhum dos dois é adivinhado
    silenciosamente: quando os dois falham, a lacuna é devolvida.
    """
    problems: list[str] = []
    for field, value in (("regionCode", region), ("location", region)):
        items, error = _query(
            pricing_client, service_code, field, value, max_pages=max_pages
        )
        if error is None:
            if items:
                return items, problems
            problems.append(
                f"{service_code}: filtro {field}={value} não devolveu item de preço"
            )
            continue
        problems.append(f"{service_code}: filtro {field} falhou ({error})")
    return [], problems


def _query(
    pricing_client, service_code: str, field: str, value: str, *, max_pages: int
) -> tuple[list[PriceItem], str | None]:
    items: list[PriceItem] = []
    token = None
    try:
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "ServiceCode": service_code,
                "Filters": [
                    {"Type": "TERM_MATCH", "Field": field, "Value": value}
                ],
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            response = pricing_client.get_products(**kwargs)
            for raw in response.get("PriceList", []):
                items.extend(_parse(raw, service_code))
            token = response.get("NextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 - categoria basta, mensagem não
        return [], type(exc).__name__
    return items, None


def _parse(raw: Any, service_code: str) -> list[PriceItem]:
    """Extrai os preços on-demand em USD de um item da Price List.

    A API devolve cada produto como uma string JSON com termos aninhados. Um
    produto pode ter mais de uma dimensão de preço (faixas de uso), e cada uma
    vira um item — quem mapeia decide qual interessa.
    """
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []

    product = payload.get("product") or {}
    attributes = {
        str(key): str(value)
        for key, value in (product.get("attributes") or {}).items()
    }
    sku = str(product.get("sku") or "")

    out: list[PriceItem] = []
    on_demand = ((payload.get("terms") or {}).get("OnDemand") or {})
    for term in on_demand.values():
        for dimension in (term.get("priceDimensions") or {}).values():
            price = (dimension.get("pricePerUnit") or {}).get("USD")
            if price is None:
                continue
            try:
                amount = float(price)
            except (TypeError, ValueError):
                continue
            out.append(
                PriceItem(
                    sku=sku,
                    service_code=service_code,
                    attributes=attributes,
                    usd_per_unit=amount,
                    unit=str(dimension.get("unit") or ""),
                    description=str(dimension.get("description") or ""),
                    effective_date=str(term.get("effectiveDate") or ""),
                )
            )
    return out


def distinct_attributes(items: list[PriceItem], keys: tuple[str, ...]) -> list[dict]:
    """Resumo para inspeção: combinações de atributos e o preço de cada uma."""
    seen: dict[tuple, dict] = {}
    for item in items:
        signature = tuple(item.attributes.get(key, "") for key in keys)
        row = seen.setdefault(
            signature,
            {
                **{key: item.attributes.get(key, "") for key in keys},
                "unit": item.unit,
                "prices": set(),
            },
        )
        row["prices"].add(item.usd_per_unit)
    return [
        {**row, "prices": sorted(row.pop("prices"))} for row in seen.values()
    ]
