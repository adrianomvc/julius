"""Preço é dado versionado, com procedência, e não mora no código.

Antes as tarifas eram literais numa dataclass com um campo `region` que ninguém
garantia ser coerente com elas: dava para rodar com preço de uma região sob o
rótulo de outra, e o relatório carimbava o rótulo.
"""

from __future__ import annotations

import tomllib

import pytest

from julius.config import DEFAULT_CONFIG
from julius.knowledge.pricing import (
    DEFAULT_REGION,
    Pricing,
    UnknownPricingRegionError,
    available_regions,
)
from julius.knowledge.pricing.rates import TABLES

# Campo de `Pricing` → onde ele tem que estar declarado na tabela.
RATE_FIELDS = {
    "glue_dpu_hour": ("glue", "dpu_hour"),
    "glue_flex_dpu_hour": ("glue", "flex_dpu_hour"),
    "glue_ray_mdpu_hour": ("glue", "ray_mdpu_hour"),
    "databrew_node_hour": ("glue", "databrew_node_hour"),
    "athena_per_tb_usd": ("athena", "per_tb"),
    "sfn_standard_per_transition": ("stepfunctions", "standard_per_transition"),
    "sfn_express_per_request": ("stepfunctions", "express_per_request"),
    "sagemaker_default_hourly": ("sagemaker", "default_hourly"),
}


def test_there_is_a_table_for_the_default_region():
    assert DEFAULT_REGION in available_regions()


@pytest.mark.parametrize("region", available_regions())
def test_each_table_declares_its_own_region(region: str):
    """O nome do arquivo e o campo `region` não podem discordar.

    É o descasamento que existia antes: preço de um lugar sob rótulo de outro.
    """
    table = tomllib.loads((TABLES / f"{region}.toml").read_text(encoding="utf-8"))
    assert table["region"] == region


@pytest.mark.parametrize("region", available_regions())
def test_each_table_carries_provenance(region: str):
    """Sem fonte citável, o número não deveria estar sendo usado."""
    pricing = Pricing.for_region(region)
    assert pricing.sources, f"{region} não declara de onde os preços vieram"
    assert pricing.version
    # `verified` pode ser falso — o que não pode é a ausência do campo passar
    # por conferido.
    assert isinstance(pricing.verified, bool)
    assert region in pricing.provenance
    if not pricing.verified:
        assert "não conferida" in pricing.provenance


@pytest.mark.parametrize("region", available_regions())
def test_every_rate_comes_from_the_table(region: str):
    """Nenhuma tarifa pode ficar valendo o default do código.

    Se um campo novo entrar em `Pricing` sem entrar na tabela, ele passaria a
    valer silenciosamente para todas as regiões.
    """
    loaded = Pricing.for_region(region)
    table = tomllib.loads((TABLES / f"{region}.toml").read_text(encoding="utf-8"))

    missing = [
        field_name
        for field_name, (section, key) in RATE_FIELDS.items()
        if key not in table.get(section, {})
    ]
    assert missing == [], (
        f"{missing} não estão em {region}.toml — valeriam o default do código"
    )
    for field_name, (section, key) in RATE_FIELDS.items():
        assert getattr(loaded, field_name) == pytest.approx(table[section][key])


def test_an_unknown_region_fails_loudly_instead_of_borrowing_prices():
    """Herdar preço de outra região produziria número sem procedência."""
    with pytest.raises(UnknownPricingRegionError) as excinfo:
        Pricing.for_region("ap-northeast-3")

    message = str(excinfo.value)
    assert "ap-northeast-3" in message
    assert "knowledge/pricing/tables" in message
    # A mensagem lista o que existe, para a correção ser óbvia.
    assert DEFAULT_REGION in message


def test_the_default_config_uses_a_table_not_literals():
    pricing = DEFAULT_CONFIG.pricing

    assert pricing.region == DEFAULT_REGION
    assert pricing.sources
    assert pricing.sagemaker_instances, "preço de instância também vem da tabela"
    # A instância desconhecida cai no default declarado na tabela.
    assert pricing.sagemaker_hourly("ml.inexistente") == pricing.sagemaker_default_hourly


def test_pricing_provenance_reaches_the_estimation_assumptions():
    """Quem lê uma estimativa modelada consegue ver de onde veio a tarifa."""
    assert "sa-east-1" in DEFAULT_CONFIG.pricing.provenance
    assert DEFAULT_CONFIG.pricing.currency == "USD"
