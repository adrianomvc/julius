"""Recomendar Glacier só quando dá para provar que o dado não é lido.

A regra existe porque `LastModified` — a única data que o S3 dá de graça — é a
da última **escrita**. Um arquivo gravado uma vez e lido todo dia parece antigo
por ela, e movê-lo para classe fria faz o time dono pagar retrieval para
reverter: a "economia" vira despesa.

Os testes aqui cobram três coisas em cima disso, e todas são maneiras de a
recomendação prometer um número que não acontece:

1. sem evidência de leitura, o achado é pergunta, não economia;
2. arquivo pequeno **encarece** ao ir para IA (cobrança mínima de 128 KB);
3. onde lifecycle ou Intelligent-Tiering já agem, recomendar é contar duas vezes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from julius.collection.models import (
    Account,
    AthenaQuery,
    S3Bucket,
    S3BucketConfig,
    S3CostCoverage,
    S3Prefix,
    Table,
)
from julius.config import DEFAULT_CONFIG
from julius.knowledge.pricing import Pricing
from julius.knowledge.rules.s3 import storage_class

_GB = 1024**3
_KB = 1024

#: Tarifas plausíveis só para o teste: o que se verifica é a fórmula, não o
#: preço. A tabela real vem do `julius pricing refresh`.
_PRECOS = {
    "standard": 0.0405,
    "standard_ia": 0.0220,
    "glacier_ir": 0.0100,
    "glacier_flexible": 0.0045,
}


def _config(**overrides):
    pricing = replace(
        Pricing.for_region("sa-east-1"),
        verified=True,
        verified_at="2026-07-29",
        verification={
            "s3": {"verified": True, "verified_at": "2026-07-29"}
        },
        s3_storage_gb_month=dict(_PRECOS),
        s3_request_per_1000={
            "copy_standard_ia": 0.01,
            "copy_glacier_ir": 0.01,
            "copy_glacier_flexible": 0.01,
            "list": 0.007,
        },
        s3_retrieval_per_gb={
            "standard_ia": 0.0,
            "glacier_ir": 0.0,
            "glacier_flexible": 0.0,
        },
        s3_retrieval_request_per_1000={
            "standard_ia": 0.0,
            "glacier_ir": 0.0,
            "glacier_flexible": 0.0,
        },
    )
    return replace(DEFAULT_CONFIG, pricing=pricing, **overrides)


def _prefixo(**overrides) -> S3Prefix:
    base = {
        "bucket": "lake",
        "prefix": "vendas/",
        "kind": "table_location",
        "source_asset": "db.vendas",
        "object_count": 5000,
        "total_bytes": 500 * _GB,
        "average_object_bytes": 100 * 1024 * 1024,
        "bytes_by_class": {"STANDARD": float(500 * _GB)},
        "object_count_by_class": {"STANDARD": 5000},
    }
    return S3Prefix(**{**base, **overrides})


def _conta(*, lido_em: str = "", prefixos=None, configs=None, **overrides) -> Account:
    measured_prefixes = prefixos if prefixos is not None else [_prefixo()]
    if lido_em:
        measured_prefixes = [
            replace(
                item,
                last_read_at=item.last_read_at or lido_em,
                read_coverage_days=item.read_coverage_days or 1000,
                access_source=item.access_source or "persisted_touch_history",
                access_quality=(
                    item.access_quality
                    if item.access_quality != "unavailable"
                    else "measured"
                ),
            )
            for item in measured_prefixes
        ]
    return Account(
        account_id="123456789012",
        window_end="2026-07-29",
        tables=[
            Table(name="db.vendas", location="s3://lake/vendas/", last_read_at=lido_em)
        ],
        s3_prefixes=measured_prefixes,
        s3_bucket_configs=configs if configs is not None else [],
        **overrides,
    )


# ---------------------------------------------------------------------------
# Sem evidência de leitura não há economia
# ---------------------------------------------------------------------------


def test_without_a_read_date_the_finding_is_a_question_not_a_saving():
    conta = _conta()  # nenhuma leitura registrada

    assert storage_class.detect(conta, _config(), "scan") == []

    sinais = storage_class.signals(conta, _config())
    assert [s.rule_id for s in sinais] == ["S3-COLD-DATA-REWRITE"]
    assert sinais[0].question and sinais[0].missing_evidence


def test_the_signal_names_what_to_enable_when_nothing_is_enabled():
    conta = _conta(configs=[S3BucketConfig(bucket="lake", access_logging_enabled=False)])

    sinal = storage_class.signals(conta, _config())[0]

    assert any("server access logging" in item for item in sinal.missing_evidence)


def test_the_signal_says_to_consult_the_source_that_is_already_on():
    """Fonte habilitada muda a próxima ação: consultar, não habilitar."""
    conta = _conta(
        configs=[S3BucketConfig(bucket="lake", access_logging_enabled=True)]
    )

    sinal = storage_class.signals(conta, _config())[0]

    assert any("já habilitada" in item for item in sinal.missing_evidence)


def test_a_recently_read_prefix_produces_nothing_at_all():
    """Nem oportunidade, nem pergunta: a resposta já existe e é "é lido"."""
    conta = _conta(lido_em="2026-07-20T00:00:00+00:00")

    assert storage_class.detect(conta, _config(), "scan") == []
    assert storage_class.signals(conta, _config()) == []


def test_a_long_unread_prefix_becomes_an_opportunity():
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00")  # ~210 dias

    achados = storage_class.detect(conta, _config(), "scan")

    assert [a.rule_id for a in achados] == ["S3-STORAGE-CLASS-TRANSITION"]
    assert achados[0].asset_name == "s3://lake/vendas/"
    assert achados[0].estimated_gain.monthly_expected > 0


def test_the_read_date_from_athena_without_coverage_remains_a_signal():
    """Uma data isolada não comprova ausência de leituras na janela inteira."""
    conta = _conta()
    conta.athena_queries = [
        AthenaQuery(
            query_id="q1",
            reads_tables=["db.vendas"],
            last_execution_at="2026-01-01T00:00:00+00:00",
        )
    ]
    from julius.collection.collectors.last_read import apply_last_read

    apply_last_read(conta)

    achados = storage_class.detect(conta, _config(), "scan")

    assert achados == []
    assert len(storage_class.signals(conta, _config())) == 1


# ---------------------------------------------------------------------------
# A conta: o que a AWS cobra além da tarifa
# ---------------------------------------------------------------------------


def test_the_transition_is_one_time_not_subtracted_from_every_month():
    """A transição reduz o primeiro mês, não a economia recorrente inteira."""
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00")  # ~210 dias

    achado = storage_class.detect(conta, _config(), "scan")[0]

    delta = _PRECOS["standard"] - _PRECOS["glacier_ir"]
    bruto = delta * 500
    transicao = 0.01 * 5000 / 1000
    assert achado.estimation.estimated_saving == pytest.approx(round(bruto, 2))
    assert achado.estimation.one_time_cost == pytest.approx(transicao)
    assert achado.estimation.first_month_net_saving == pytest.approx(
        round(bruto - transicao, 2)
    )
    assert achado.estimation.break_even_months == pytest.approx(
        round(transicao / bruto, 3)
    )
    assert any("custo pontual" in item for item in achado.estimation.assumptions)


def test_a_small_file_prefix_is_never_recommended():
    """A AWS fatura 128 KB por objeto em IA e Glacier IR.

    Um prefixo com milhões de arquivos de 4 KB pagaria 32x mais espaço faturado
    depois da transição: a recomendação anunciaria economia e produziria conta.
    """
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[_prefixo(average_object_bytes=4 * _KB)],
    )

    assert storage_class.detect(conta, _config(), "scan") == []


def test_an_unmeasured_object_size_is_not_a_reason_to_recommend():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[_prefixo(average_object_bytes=None)],
    )

    assert storage_class.detect(conta, _config(), "scan") == []


@pytest.mark.parametrize(
    ("lido_em", "classe"),
    [
        # Os cortes são o dobro do mínimo de retenção de cada classe. A folga é
        # deliberada: mover no limite significa que uma releitura no mês
        # seguinte paga o período inteiro sem ter economizado nada.
        ("2026-04-01T00:00:00+00:00", "Standard-IA"),              # ~119 dias
        ("2026-01-01T00:00:00+00:00", "Glacier Instant Retrieval"),  # ~210 dias
        ("2025-01-01T00:00:00+00:00", "Glacier Flexible Retrieval"),  # ~575 dias
    ],
)
def test_the_longer_it_sits_the_colder_the_class(lido_em, classe):
    conta = _conta(lido_em=lido_em)

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert classe in achado.recommended_action


def test_deep_archive_is_never_chosen_by_the_rule():
    """Retenção de 180 dias e horas até o primeiro byte não são escolha de regra."""
    conta = _conta(lido_em="2020-01-01T00:00:00+00:00")

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert "Deep Archive" not in achado.recommended_action


def test_a_prefix_just_below_the_cold_threshold_is_not_recommended():
    """Abaixo da janela, ausência de leitura é falta de observação."""
    conta = _conta(lido_em="2026-06-01T00:00:00+00:00")  # ~58 dias

    assert storage_class.detect(conta, _config(), "scan") == []


def test_retrieval_cost_is_declared_as_a_risk_not_hidden():
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00")

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert any("retrieval" in risco for risco in achado.risks)
    assert any("retenção" in risco for risco in achado.risks)


def test_without_an_s3_price_table_the_finding_is_blocked_not_invented():
    """Tarifa ausente não pode virar economia zero com cara de conclusão."""
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00")
    sem_preco = replace(
        _config(), pricing=replace(Pricing.for_region("sa-east-1"), s3_storage_gb_month={})
    )

    achado = storage_class.detect(conta, sem_preco, "scan")[0]

    assert achado.blocked is True
    assert achado.estimation.saving_quality == "unavailable"
    assert any("pricing refresh" in item for item in achado.estimation.assumptions)


# ---------------------------------------------------------------------------
# Onde já há automação, recomendar é cobrar duas vezes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config_bucket",
    [
        S3BucketConfig(bucket="lake", intelligent_tiering_ids=["auto"]),
        S3BucketConfig(
            bucket="lake", lifecycle_rules=[{"ID": "frio", "Transitions": [{"Days": 90}]}]
        ),
    ],
)
def test_a_bucket_that_already_transitions_is_left_alone(config_bucket):
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00", configs=[config_bucket])

    assert storage_class.detect(conta, _config(), "scan") == []
    assert storage_class.signals(conta, _config()) == []


def test_an_expiration_only_lifecycle_does_not_suppress_the_finding():
    """Expirar apaga; não move de classe. A recomendação continua valendo."""
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        configs=[
            S3BucketConfig(
                bucket="lake", lifecycle_rules=[{"ID": "apaga", "Expiration": {"Days": 30}}]
            )
        ],
    )

    assert len(storage_class.detect(conta, _config(), "scan")) == 1


# ---------------------------------------------------------------------------
# O que já é frio, e o que é pequeno demais para valer a conversa
# ---------------------------------------------------------------------------


def test_data_already_in_a_cold_class_is_not_moved_again():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[
            _prefixo(
                bytes_by_class={"GLACIER": float(500 * _GB)},
                object_count_by_class={"GLACIER": 5000},
            )
        ],
    )

    assert storage_class.detect(conta, _config(), "scan") == []


def test_only_the_hot_bytes_are_counted_in_a_mixed_prefix():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[
            _prefixo(
                bytes_by_class={
                    "STANDARD": float(300 * _GB),
                    "GLACIER": float(200 * _GB),
                },
                object_count_by_class={"STANDARD": 3000, "GLACIER": 2000},
            )
        ],
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    delta = _PRECOS["standard"] - _PRECOS["glacier_ir"]
    esperado = delta * 300
    assert achado.estimation.estimated_saving == pytest.approx(round(esperado, 2), abs=0.01)


def test_prefix_contract_access_evidence_is_consumed_before_catalog_fallback():
    conta = _conta(
        prefixos=[
            _prefixo(
                last_read_at="2026-01-01T00:00:00+00:00",
                access_source="server_access_logs",
                access_quality="best_effort",
                read_coverage_days=210,
                read_requests_window=0,
                bytes_read_window=0,
            )
        ]
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert "server_access_logs" in " ".join(achado.evidence)
    assert achado.estimation.method == "s3_storage_class_transition_v2"


def test_old_object_access_date_without_cold_window_coverage_is_only_a_signal():
    conta = _conta(
        prefixos=[
            _prefixo(
                last_read_at="2026-01-01T00:00:00+00:00",
                access_source="server_access_logs",
                access_quality="best_effort",
                read_coverage_days=30,
            )
        ]
    )

    assert storage_class.detect(conta, _config(), "scan") == []
    assert len(storage_class.signals(conta, _config())) == 1


def test_observed_zero_reads_needs_complete_coverage():
    completo = _prefixo(
        access_source="server_access_logs",
        access_quality="best_effort",
        read_coverage_days=120,
        read_requests_window=0,
        bytes_read_window=0,
    )
    parcial = _prefixo(
        prefix="parcial/",
        source_asset="db.parcial",
        access_source="server_access_logs",
        access_quality="partial",
        read_coverage_days=120,
        read_requests_window=0,
        bytes_read_window=0,
    )
    conta = _conta(prefixos=[completo, parcial])

    assert len(storage_class.detect(conta, _config(), "scan")) == 1
    assert len(storage_class.signals(conta, _config())) == 1


def test_expiration_before_break_even_suppresses_double_counting():
    costly = _config()
    costly = replace(
        costly,
        pricing=replace(
            costly.pricing,
            s3_request_per_1000={
                **costly.pricing.s3_request_per_1000,
                "copy_glacier_ir": 1000.0,
            },
        ),
    )
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[_prefixo(oldest_object_age_days=100)],
        configs=[
            S3BucketConfig(
                bucket="lake",
                lifecycle_rules=[
                    {"ID": "apaga", "Expiration": {"Days": 120}}
                ],
            )
        ],
    )

    assert storage_class.detect(conta, costly, "scan") == []


def test_overlapping_prefixes_are_not_counted_twice():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[
            _prefixo(prefix="vendas/"),
            _prefixo(prefix="vendas/ano=2025/", source_asset="db.vendas_2025"),
        ],
    )

    achados = storage_class.detect(conta, _config(), "scan")

    assert [item.asset_name for item in achados] == [
        "s3://lake/vendas/ano=2025/"
    ]


def test_lifecycle_filter_only_suppresses_the_prefix_it_reaches():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[
            _prefixo(
                prefix="vendas/", last_read_at="2026-01-01T00:00:00+00:00"
            ),
            _prefixo(
                prefix="estoque/", last_read_at="2026-01-01T00:00:00+00:00"
            ),
        ],
        configs=[
            S3BucketConfig(
                bucket="lake",
                lifecycle_rules=[
                    {
                        "ID": "vendas-frio",
                        "Filter": {"Prefix": "vendas/"},
                        "Transitions": [{"Days": 90}],
                    }
                ],
            )
        ],
    )

    achados = storage_class.detect(conta, _config(), "scan")

    assert [item.asset_name for item in achados] == ["s3://lake/estoque/"]


def test_glacier_flexible_requires_human_recovery_sla_confirmation():
    conta = _conta(lido_em="2025-01-01T00:00:00+00:00")

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert achado.blocked is True
    assert any("SLA" in item for item in achado.missing_evidence)


def test_size_distribution_drives_target_billable_bytes():
    physical = 500 * _GB
    objects = 5_000_000
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[
            _prefixo(
                object_count=objects,
                object_count_by_class={"STANDARD": objects},
                bytes_by_class_size={
                    "STANDARD": {
                        "1-128kb": float(4_000_000 * 1024),
                        "1-64mb": float(physical - 4_000_000 * 1024),
                    }
                },
                object_count_by_class_size={
                    "STANDARD": {
                        "1-128kb": 4_000_000,
                        "1-64mb": 1_000_000,
                    }
                },
            )
        ],
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert achado.estimation.projected_bytes > physical


def test_standard_baseline_is_anchored_in_reconciled_billing():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        s3_buckets=[
            S3Bucket(
                name="lake",
                bytes_by_class={"StandardStorage": float(1000 * _GB)},
            )
        ],
        s3_cost_coverage=S3CostCoverage(
            buckets={"storage_standard": 100.0},
            cost_quality="reconciled",
        ),
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert achado.estimation.baseline_cost == 50.0
    assert achado.estimation.baseline_quality == "allocated"
    assert achado.estimation.saving_quality == "allocated_partial"


def test_a_prefix_too_small_to_be_worth_the_conversation_is_skipped():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[
            _prefixo(
                bytes_by_class={"STANDARD": float(1 * _GB)},
                object_count_by_class={"STANDARD": 10},
            )
        ],
    )

    assert storage_class.detect(conta, _config(), "scan") == []


# ---------------------------------------------------------------------------
# O Julius recomenda; quem executa é o time dono
# ---------------------------------------------------------------------------


def test_the_recommendation_says_who_rewrites():
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00")

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert "não executa" in achado.how_to_apply
    assert "CopyObject" in achado.how_to_apply
    assert "lifecycle" not in achado.how_to_apply.lower()


def test_a_truncated_listing_says_the_volume_is_a_floor():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[_prefixo(listing_complete=False)],
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert any("parcial" in item for item in achado.missing_evidence)
    assert achado.include_in_portfolio is False
    assert any("parcial" in risco for risco in achado.risks)


def test_copy_on_versioned_bucket_is_blocked_until_noncurrent_cost_is_known():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        s3_buckets=[
            S3Bucket(
                name="lake",
                bytes_by_class={"StandardStorage": float(500 * _GB)},
                versioning_enabled=True,
            )
        ],
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert achado.blocked is True
    assert achado.include_in_portfolio is False
    assert any("versões não correntes" in item for item in achado.missing_evidence)


def test_age_is_evidence_and_never_sizes_the_transition():
    """Cruzar idade com classe exigiria uma distribuição conjunta que ninguém mede.

    `bytes_by_age` e `bytes_by_class` são marginais. Usar as duas para separar
    "bytes que compensam transitar" de "bytes que expiram antes do payback"
    assumiria que a idade se distribui igual entre as classes — e o erro dessa
    suposição cairia direto na cifra. Como evidência, a mesma marginal é honesta.
    """
    from julius.knowledge.rules.s3.storage_class import _perfil_de_idade

    class _Prefixo:
        bytes_by_age = {"0-30": 100.0, "180-365": 300.0, "365+": 600.0}
        object_count_by_age = {"0-30": 1, "180-365": 3, "365+": 6}

    class _Vazio:
        bytes_by_age: dict = {}
        object_count_by_age: dict = {}

    linhas = _perfil_de_idade(_Prefixo())

    assert len(linhas) == 1
    assert "90% dos bytes com mais de 180 dias" in linhas[0]
    # Sem listagem por idade, o silêncio é melhor que um percentual inventado.
    assert _perfil_de_idade(_Vazio()) == []
