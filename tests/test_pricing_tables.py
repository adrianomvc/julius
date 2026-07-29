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


# --------------------------------------------------------------------------
# S3: a tarifa que compara duas opções, e por isso não tem default
# --------------------------------------------------------------------------


def test_a_missing_s3_table_says_so_instead_of_guessing():
    """Sem tarifa de S3, a comparação entre classes não pode acontecer.

    As outras tarifas têm fallback porque modelam um custo que já existia. Esta
    decide se vale mover petabytes para Glacier, e um default chutado não daria
    uma estimativa imprecisa — daria uma recomendação inventada com cifra ao
    lado. O vazio é o que faz a regra sair como sinal.
    """
    pricing = Pricing.for_region(DEFAULT_REGION)

    if pricing.has_s3_storage_rates:
        pytest.skip("a tabela já foi populada por `julius pricing refresh`")

    assert pricing.s3_storage_gb_month == {}
    assert pricing.s3_storage_delta("glacier_flexible") is None
    assert pricing.s3_request_cost("list", 1_000_000) is None


def test_the_saving_per_gb_is_the_difference_not_the_target_price():
    """Mover para Glacier não economiza o preço do Glacier: economiza a diferença.

    Trocar um pelo outro superestimaria a economia em ~4x no caso Standard →
    Glacier, e o erro sairia como número exato no relatório.
    """
    from dataclasses import replace

    pricing = replace(
        Pricing.for_region(DEFAULT_REGION),
        s3_storage_gb_month={"standard": 0.0405, "glacier_flexible": 0.0045},
    )

    assert pricing.has_s3_storage_rates is True
    assert pricing.s3_storage_delta("glacier_flexible") == pytest.approx(0.036)
    # Classe que a tabela não conhece continua sendo `None`, não zero: zero se
    # leria como "não compensa".
    assert pricing.s3_storage_delta("deep_archive") is None


def test_request_cost_scales_per_thousand():
    from dataclasses import replace

    pricing = replace(
        Pricing.for_region(DEFAULT_REGION),
        s3_request_per_1000={"list": 0.007, "lifecycle_transition": 0.01},
    )

    # Um data lake de 10 milhões de objetos são 10 mil chamadas de LIST.
    assert pricing.s3_request_cost("list", 10_000) == pytest.approx(0.07)
    assert pricing.s3_request_cost("lifecycle_transition", 10_000_000) == pytest.approx(100.0)
    assert pricing.s3_request_cost("get", 1000) is None


def test_retrieval_cost_combines_volume_and_requests():
    from dataclasses import replace

    pricing = replace(
        Pricing.for_region(DEFAULT_REGION),
        s3_retrieval_per_gb={"glacier_ir": 0.03},
        s3_retrieval_request_per_1000={"glacier_ir": 0.01},
    )

    assert pricing.s3_retrieval_cost(
        "glacier_ir", bytes_read=10 * 1024**3, requests=2_000
    ) == pytest.approx(0.32)
    assert pricing.s3_retrieval_cost(
        "standard_ia", bytes_read=10 * 1024**3, requests=2_000
    ) is None


def test_the_s3_mapping_covers_every_class_the_rule_can_recommend():
    """Entrada faltando no mapa vira tarifa ausente depois do refresh."""
    from julius.knowledge.pricing.refresh import load_mapping

    s3 = load_mapping().get("s3", {})

    esperadas = {
        "storage_standard",
        "storage_standard_ia",
        "storage_onezone_ia",
        "storage_glacier_ir",
        "storage_glacier_flexible",
        "storage_deep_archive",
        "request_list_per_1000",
        "request_get_per_1000",
        "request_lifecycle_transition_per_1000",
    }
    assert esperadas <= set(s3)
    assert all(entry.get("service") == "AmazonS3" for entry in s3.values())


def test_a_new_mapping_section_is_not_silently_dropped_when_writing():
    """O renderer listava as seções à mão, e a nova resolveria sem ser escrita."""
    from datetime import date

    from julius.knowledge.pricing.refresh import Resolution, render_table

    texto = render_table(
        "sa-east-1",
        [Resolution("s3", "storage_standard", "AmazonS3", value=0.0405)],
        today=date(2026, 7, 29),
        sagemaker_default=0.18,
    )

    assert "[s3]" in texto
    assert "storage_standard = 0.0405" in texto
