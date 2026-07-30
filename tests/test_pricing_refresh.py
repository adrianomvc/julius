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

#: Armazenamento S3 chega em faixas decrescentes por volume (primeiros 50 TB,
#: próximos 450 TB, acima de 500 TB). `pick = "min"` pega a mais barata — que só
#: vale para quem passa de 500 TB — e é justamente por isso que ela é a escolha
#: certa aqui: subestima o preço do Standard, subestima a diferença entre
#: classes e portanto subestima a economia. Errar prometendo menos é o lado
#: seguro de uma recomendação que pede para mover petabytes.
S3 = [
    _product(
        "S3STD",
        {"productFamily": "Storage", "volumeType": "Standard"},
        [0.0405, 0.0389, 0.0374],
        unit="GB-Mo",
    ),
]


def _sm(component: str, instance: str, price: float) -> str:
    return _product(
        f"SM{component}{instance}".replace(".", ""),
        {
            "productFamily": "ML Instance",
            "component": component,
            "instanceName": instance,
        },
        [price],
    )


SAGEMAKER = [
    _sm("Studio Notebook", "ml.t3.medium", 0.05),
    _sm("Studio Notebook", "ml.m5.large", 0.12),
    _sm("Training", "ml.m5.xlarge", 0.23),
]

_MAPA_SAGEMAKER = (
    '[sagemaker.studio]\nservice = "AmazonSageMaker"\n'
    'match = { productFamily = "ML Instance", component = "Studio Notebook" }\n'
    'expand = "instanceName"\npick = "min"\n'
)


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
    assert table["verified_at"] == TODAY.isoformat()
    assert table["effective_date"] == ""
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


# --------------------------------------------------------------------------
# Atualizar uma parte do mapa sem perder o resto
# --------------------------------------------------------------------------

_MAPA_DUAS_SECOES = (
    '[glue.dpu_hour]\nservice = "AWSGlue"\n'
    'match = { group = "ETL Job run", operation = "jobrun" }\npick = "only"\n'
    '\n[s3.storage_standard]\nservice = "AmazonS3"\n'
    'match = { productFamily = "Storage", volumeType = "Standard" }\npick = "min"\n'
)


def _tabela_existente(tmp_path):
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "sa-east-1.toml").write_text(
        'region = "sa-east-1"\ncurrency = "USD"\nversion = "antiga"\n'
        "verified = true\n"
        "\n[glue]\ndpu_hour = 0.44\nflex_dpu_hour = 0.29\n"
        "\n[athena]\nper_tb = 5.0\n"
        "\n[sagemaker]\ndefault_hourly = 0.18\n",
        encoding="utf-8",
    )
    mapping_file = tmp_path / "mapping.toml"
    mapping_file.write_text(_MAPA_DUAS_SECOES, encoding="utf-8")
    return tables, mapping_file


def test_refreshing_one_section_does_not_erase_the_others(tmp_path):
    """`--only s3` não pode apagar Glue e Athena, conferidos antes.

    O renderizador escreve o que o mapa resolveu. Sem carregar o resto da tabela
    atual, uma execução que sequer consultou a Athena a apagaria — e o
    `verified = true` continuaria lá, agora sobre uma tabela mutilada.
    """
    tables, mapping_file = _tabela_existente(tmp_path)

    written, resolutions = refresh_region(
        FakePricing({"AmazonS3": S3}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
        sections=("s3",),
    )

    assert written is not None
    table = tomllib.loads(written.read_text(encoding="utf-8"))
    assert table["s3"]["storage_standard"] == pytest.approx(0.0374)
    # O que não foi buscado continua lá.
    assert table["athena"]["per_tb"] == pytest.approx(5.0)
    assert table["glue"]["flex_dpu_hour"] == pytest.approx(0.29)
    # E só a seção pedida foi consultada.
    assert [item.section for item in resolutions] == ["s3"]


def test_an_unverified_new_section_does_not_block_the_rest(tmp_path):
    """Foi o que aconteceu ao acrescentar S3: nove palpites travavam tudo.

    A atomicidade protege o `verified`; ela não deveria transformar mapeamento
    novo em bloqueio para o que já funcionava.
    """
    tables, mapping_file = _tabela_existente(tmp_path)

    written, _ = refresh_region(
        FakePricing({"AWSGlue": GLUE}),  # sem produtos de S3: a seção falharia
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
        sections=("glue",),
    )

    assert written is not None
    assert tomllib.loads(written.read_text(encoding="utf-8"))["glue"]["dpu_hour"]


def test_an_unknown_section_fails_loudly_instead_of_refreshing_nothing(tmp_path):
    tables, mapping_file = _tabela_existente(tmp_path)

    with pytest.raises(ValueError, match="inexistentes"):
        refresh_region(
            FakePricing({}),
            "sa-east-1",
            today=TODAY,
            tables=tables,
            mapping_path=mapping_file,
            sections=("s4",),
        )


def test_dry_run_reports_without_touching_the_table(tmp_path):
    """O laço de conferência: ver o que casa antes de escrever."""
    tables, mapping_file = _tabela_existente(tmp_path)
    antes = (tables / "sa-east-1.toml").read_text(encoding="utf-8")

    written, resolutions = refresh_region(
        FakePricing({"AmazonS3": S3}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
        sections=("s3",),
        dry_run=True,
    )

    assert written is None
    assert all(item.ok for item in resolutions)
    assert (tables / "sa-east-1.toml").read_text(encoding="utf-8") == antes


# --------------------------------------------------------------------------
# Tarifa por tipo de instância: uma decisão humana, muitos valores
# --------------------------------------------------------------------------


class _Anterior:
    """O que `Pricing.for_region` devolve para a tabela de antes do mapa.

    `refresh_region` recebe isso por injeção (`previous`); sem passar, nada é
    carregado e um teste de preservação passaria vazio.
    """

    sagemaker_instances = {"ml.t3.medium": 0.05, "ml.p3.2xlarge": 4.0}
    sagemaker_component_instances = {"studio": {"ml.t3.medium": 0.05}}
    sagemaker_default_hourly = 0.18


def _tabela_com_instancias_a_mao(tmp_path):
    """Tabela no estado anterior ao mapa: instâncias escritas à mão."""
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "sa-east-1.toml").write_text(
        'region = "sa-east-1"\ncurrency = "USD"\nversion = "antiga"\n'
        "verified = false\n"
        "\n[glue]\ndpu_hour = 0.44\n"
        "\n[sagemaker]\ndefault_hourly = 0.18\n"
        '\n[sagemaker.instances]\n"ml.t3.medium" = 0.05\n"ml.p3.2xlarge" = 4.0\n',
        encoding="utf-8",
    )
    mapping_file = tmp_path / "mapping.toml"
    mapping_file.write_text(_MAPA_SAGEMAKER, encoding="utf-8")
    return tables, mapping_file


def test_one_entry_resolves_a_rate_per_instance_type(tmp_path):
    """Mapear tipo por tipo seria copiar a mesma decisão dezenas de vezes.

    E deixaria tipo novo na conta sem tarifa — que, com o gate de pricing, é
    cifra bloqueada por omissão do mapa, não por falta de evidência.
    """
    tables, mapping_file = _tabela_com_instancias_a_mao(tmp_path)

    written, resolutions = refresh_region(
        FakePricing({"AmazonSageMaker": SAGEMAKER}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
        previous=lambda _region: _Anterior,
    )

    assert written is not None
    assert all(item.ok for item in resolutions)
    table = tomllib.loads(written.read_text(encoding="utf-8"))
    studio = table["sagemaker"]["components"]["studio"]
    assert studio["ml.t3.medium"] == pytest.approx(0.05)
    assert studio["ml.m5.large"] == pytest.approx(0.12)
    # O componente que o mapa não pediu não entra de carona.
    assert "training" not in table["sagemaker"]["components"]


def test_refreshing_sagemaker_drops_the_hand_written_instance_list(tmp_path):
    """A lista antiga não pode herdar a procedência que este refresh conferiu.

    Ela é o default escrito à mão de antes do mapa. Mantê-la dentro de uma
    seção agora marcada como conferida daria carimbo de Price List a uma tarifa
    que ninguém checou — e `sagemaker_hourly` a usaria como fallback silencioso.
    """
    tables, mapping_file = _tabela_com_instancias_a_mao(tmp_path)

    written, _ = refresh_region(
        FakePricing({"AmazonSageMaker": SAGEMAKER}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
        previous=lambda _region: _Anterior,
    )

    table = tomllib.loads(written.read_text(encoding="utf-8"))
    assert "instances" not in table["sagemaker"]
    # ml.p3.2xlarge não estava na resposta da API: fica sem tarifa, e a regra
    # bloqueia a cifra em vez de usar o número antigo.
    assert "ml.p3.2xlarge" not in table["sagemaker"]["components"]["studio"]
    assert table["verification"]["sagemaker"]["verified"] is True


def test_refreshing_another_section_keeps_the_sagemaker_components(tmp_path):
    """`--only glue` não pode apagar tarifa de instância já conferida."""
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "sa-east-1.toml").write_text(
        'region = "sa-east-1"\ncurrency = "USD"\nversion = "antiga"\n'
        "verified = true\n"
        "\n[glue]\ndpu_hour = 0.44\nflex_dpu_hour = 0.29\n"
        "\n[athena]\nper_tb = 5.0\n"
        "\n[stepfunctions]\nstandard_per_transition = 2.5e-05\n"
        "express_per_request = 1e-06\n"
        "\n[sagemaker]\ndefault_hourly = 0.18\n"
        '\n[sagemaker.components.studio]\n"ml.t3.medium" = 0.05\n',
        encoding="utf-8",
    )
    mapping_file = tmp_path / "mapping.toml"
    mapping_file.write_text(_MAPA_DUAS_SECOES, encoding="utf-8")

    written, _ = refresh_region(
        FakePricing({"AWSGlue": GLUE}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
        sections=("glue",),
        previous=lambda _region: _Anterior,
    )

    table = tomllib.loads(written.read_text(encoding="utf-8"))
    assert table["sagemaker"]["components"]["studio"]["ml.t3.medium"] == pytest.approx(
        0.05
    )
    # E a seção não refrescada mantém a lista antiga: descartá-la é decisão de
    # quem conferiu a seção, não efeito colateral de atualizar Glue.
    assert table["sagemaker"]["instances"]["ml.p3.2xlarge"] == pytest.approx(4.0)


def test_an_ambiguous_instance_rate_refuses_the_whole_entry():
    """Ambiguidade em um tipo reprova a entrada, não escolhe por conta própria."""
    client = FakePricing(
        {
            "AmazonSageMaker": [
                _sm("Studio Notebook", "ml.t3.medium", 0.05),
                _product(
                    "SMDUP",
                    {
                        "productFamily": "ML Instance",
                        "component": "Studio Notebook",
                        "instanceName": "ml.t3.medium",
                    },
                    [0.09],
                ),
            ]
        }
    )
    items, _ = fetch_products(client, "AmazonSageMaker", "sa-east-1")
    mapping = tomllib.loads(_MAPA_SAGEMAKER.replace('pick = "min"', 'pick = "only"'))

    resolutions = resolve(mapping, {"AmazonSageMaker": items})

    assert not resolutions[0].ok
    assert "instanceName=ml.t3.medium" in resolutions[0].problem


def test_an_expanded_entry_that_matches_nothing_blocks_the_write(tmp_path):
    tables, mapping_file = _tabela_com_instancias_a_mao(tmp_path)

    written, resolutions = refresh_region(
        FakePricing({"AmazonSageMaker": [_sm("Training", "ml.m5.large", 0.12)]}),
        "sa-east-1",
        today=TODAY,
        tables=tables,
        mapping_path=mapping_file,
    )

    assert written is None
    assert "nenhum preço com atributo instanceName" in resolutions[0].problem


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


def test_every_declared_pricing_dependency_can_actually_be_verified():
    """Seção declarada e não mapeada é cifra bloqueada para sempre.

    `dependencies_are_current` reprova a seção ausente do mapa `verification`, e
    só o refresh popula esse mapa — a partir do `mapping.toml`. Uma dependência
    declarada sem entrada no mapa não falha em lugar nenhum: passa em silêncio
    rebaixando toda cifra modelada daquele serviço para `unavailable`. Foi
    exatamente o que aconteceu com SageMaker.
    """
    from julius.findings.build import _pricing_dependencies
    from julius.knowledge.pricing.refresh import load_mapping

    mapping = load_mapping()
    # Um asset_type por prefixo que `_pricing_dependencies` reconhece.
    declared = {
        section
        for asset_type in (
            "glue_job",
            "athena_workgroup",
            "state_machine",
            "sagemaker_app",
            "s3_prefix",
        )
        for section in _pricing_dependencies(asset_type)
    }

    assert declared, "o mapeamento de dependências ficou vazio"
    assert declared <= set(mapping), (
        "seções declaradas sem entrada no mapping.toml: "
        f"{sorted(declared - set(mapping))}"
    )


def test_pricing_verify_requires_every_mapped_section():
    """Verde no `pricing verify` não pode ignorar seção que sustenta cifra."""
    import inspect as inspect_mod

    from julius.cli.pricing import pricing_verify
    from julius.knowledge.pricing.refresh import load_mapping

    default = inspect_mod.signature(pricing_verify).parameters["sections"].default
    required = {item.strip() for item in str(default.default).split(",")}

    assert set(load_mapping()) <= required, (
        "seções mapeadas fora do default de `pricing verify`: "
        f"{sorted(set(load_mapping()) - required)}"
    )
