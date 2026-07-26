"""Regeneração da tabela de preço a partir da Price List API.

O mapeamento entre atributos de produto e tarifa não pode ser adivinhado no
código: varia por serviço e muda. O que estes testes garantem é o contrato ao
redor dele — que uma resolução ambígua ou vazia impede a escrita, e que nada
parcial jamais é marcado como conferido.
"""

from __future__ import annotations

import json
import tomllib
from datetime import date

import pytest

from julius.collection.collectors.pricing import (
    distinct_attributes,
    fetch_products,
)
from julius.knowledge.pricing.refresh import refresh_region, render_table, resolve

TODAY = date(2026, 7, 25)


def _product(sku: str, attributes: dict, prices: list[float], unit: str = "Hrs") -> str:
    return json.dumps(
        {
            "product": {"sku": sku, "attributes": attributes},
            "terms": {
                "OnDemand": {
                    f"{sku}.JRTCKXETXF": {
                        "priceDimensions": {
                            f"{sku}.JRTCKXETXF.{index}": {
                                "unit": unit,
                                "description": f"{sku} tier {index}",
                                "pricePerUnit": {"USD": str(price)},
                            }
                            for index, price in enumerate(prices)
                        }
                    }
                }
            },
        }
    )


class FakePricing:
    """Price List mínima: responde por serviço e registra os filtros usados."""

    def __init__(self, by_service: dict[str, list[str]], *, fail_field: str = ""):
        self.by_service = by_service
        self.fail_field = fail_field
        self.filters: list[dict] = []

    def get_products(self, **kwargs):
        self.filters.extend(kwargs["Filters"])
        if any(f["Field"] == self.fail_field for f in kwargs["Filters"]):
            raise PermissionError("acesso negado")
        return {"PriceList": self.by_service.get(kwargs["ServiceCode"], [])}


GLUE = [
    _product("GLUE1", {"group": "ETL Job run", "operation": "jobrun"}, [0.44]),
    _product("GLUE2", {"group": "ETL Job run", "operation": "jobrunflex"}, [0.29]),
]


def test_products_are_flattened_from_the_nested_price_list():
    client = FakePricing({"AWSGlue": GLUE})

    items, problems = fetch_products(client, "AWSGlue", "sa-east-1")

    assert problems == []
    assert {item.usd_per_unit for item in items} == {0.44, 0.29}
    assert all(item.unit == "Hrs" for item in items)
    # O filtro por região é `regionCode`, não o endpoint da API.
    assert client.filters[0] == {
        "Type": "TERM_MATCH",
        "Field": "regionCode",
        "Value": "sa-east-1",
    }


def test_region_filter_falls_back_to_location_and_records_the_attempt():
    client = FakePricing({"AWSGlue": GLUE}, fail_field="regionCode")

    items, problems = fetch_products(client, "AWSGlue", "sa-east-1")

    assert items, "o fallback por `location` deveria ter trazido os itens"
    assert any("regionCode" in problem for problem in problems)


def test_inspection_groups_attributes_so_a_person_can_map_them():
    client = FakePricing({"AWSGlue": GLUE})
    items, _ = fetch_products(client, "AWSGlue", "sa-east-1")

    rows = distinct_attributes(items, ("group", "operation"))

    assert len(rows) == 2
    assert {row["operation"] for row in rows} == {"jobrun", "jobrunflex"}
    assert all(row["prices"] for row in rows)


def test_an_ambiguous_match_refuses_instead_of_picking_one():
    """Dois preços casando com `pick = only` é mapa mal definido, não escolha."""
    client = FakePricing(
        {
            "AWSGlue": [
                _product("A", {"group": "ETL Job run", "operation": "jobrun"}, [0.44]),
                _product("B", {"group": "ETL Job run", "operation": "jobrun"}, [0.88]),
            ]
        }
    )
    items, _ = fetch_products(client, "AWSGlue", "sa-east-1")
    mapping = {
        "glue": {
            "dpu_hour": {
                "service": "AWSGlue",
                "match": {"group": "ETL Job run", "operation": "jobrun"},
                "pick": "only",
            }
        }
    }

    resolutions = resolve(mapping, {"AWSGlue": items})

    assert not resolutions[0].ok
    assert "2 preços distintos" in resolutions[0].problem
    assert "pick" in resolutions[0].problem


def test_nothing_is_written_when_a_rate_does_not_resolve(tmp_path):
    """Tabela parcial marcada como conferida é pior que a não conferida."""
    tables = tmp_path / "tables"
    tables.mkdir()
    existing = tables / "sa-east-1.toml"
    existing.write_text('region = "sa-east-1"\nverified = false\n', encoding="utf-8")

    mapping_file = tmp_path / "mapping.toml"
    mapping_file.write_text(
        '[glue.dpu_hour]\nservice = "AWSGlue"\n'
        'match = { group = "Inexistente" }\npick = "only"\n',
        encoding="utf-8",
    )

    written, resolutions = refresh_region(
        FakePricing({"AWSGlue": GLUE}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
    )

    assert written is None
    assert not any(item.ok for item in resolutions)
    # E o arquivo que já existia não foi tocado.
    assert "verified = false" in existing.read_text(encoding="utf-8")


def test_a_complete_refresh_writes_a_verified_and_dated_table(tmp_path):
    tables = tmp_path / "tables"
    tables.mkdir()
    mapping_file = tmp_path / "mapping.toml"
    mapping_file.write_text(
        '[glue.dpu_hour]\nservice = "AWSGlue"\n'
        'match = { group = "ETL Job run", operation = "jobrun" }\npick = "only"\n'
        '\n[glue.flex_dpu_hour]\nservice = "AWSGlue"\n'
        'match = { group = "ETL Job run", operation = "jobrunflex" }\npick = "only"\n',
        encoding="utf-8",
    )

    written, resolutions = refresh_region(
        FakePricing({"AWSGlue": GLUE}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
    )

    assert written is not None
    assert all(item.ok for item in resolutions)
    table = tomllib.loads(written.read_text(encoding="utf-8"))
    assert table["region"] == "sa-east-1"
    assert table["verified"] is True
    assert table["effective_date"] == TODAY.isoformat()
    assert table["glue"]["dpu_hour"] == pytest.approx(0.44)
    assert table["glue"]["flex_dpu_hour"] == pytest.approx(0.29)


def test_refresh_preserves_instance_prices_it_does_not_fetch():
    """O refresh não apaga dado revisado que ele não sabe buscar."""

    class Previous:
        sagemaker_instances = {"ml.m5.large": 0.12}
        sagemaker_default_hourly = 0.18

    text = render_table(
        "sa-east-1",
        [],
        today=TODAY,
        sagemaker=Previous.sagemaker_instances,
        sagemaker_default=Previous.sagemaker_default_hourly,
    )

    table = tomllib.loads(text)
    assert table["sagemaker"]["instances"]["ml.m5.large"] == pytest.approx(0.12)
    assert table["sagemaker"]["default_hourly"] == pytest.approx(0.18)


def test_the_shipped_mapping_is_loadable_and_names_real_services():
    from julius.knowledge.pricing.refresh import load_mapping

    mapping = load_mapping()
    services = {
        spec["service"] for entries in mapping.values() for spec in entries.values()
    }

    assert {"AWSGlue", "AmazonAthena", "AWSStepFunctions"} <= services
    for entries in mapping.values():
        for key, spec in entries.items():
            assert spec.get("match"), f"{key} sem critério de match"
            assert spec.get("pick") in {"only", "min", "max"}
