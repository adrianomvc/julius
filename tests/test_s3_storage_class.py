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
    S3BucketConfig,
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
        s3_storage_gb_month=dict(_PRECOS),
        s3_request_per_1000={"lifecycle_transition": 0.01, "list": 0.007},
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
    return Account(
        account_id="123456789012",
        window_end="2026-07-29",
        tables=[
            Table(name="db.vendas", location="s3://lake/vendas/", last_read_at=lido_em)
        ],
        s3_prefixes=prefixos if prefixos is not None else [_prefixo()],
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


def test_the_read_date_can_come_from_the_athena_history_alone():
    """Sem tabela de toques configurada, o histórico de queries responde."""
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

    assert len(achados) == 1


# ---------------------------------------------------------------------------
# A conta: o que a AWS cobra além da tarifa
# ---------------------------------------------------------------------------


def test_the_saving_is_the_difference_between_classes_minus_the_transition():
    """Mover para Glacier não economiza o preço do Glacier: a diferença."""
    conta = _conta(lido_em="2026-01-01T00:00:00+00:00")  # ~210 dias

    achado = storage_class.detect(conta, _config(), "scan")[0]

    delta = _PRECOS["standard"] - _PRECOS["glacier_ir"]
    bruto = delta * 500
    transicao = 0.01 * 5000 / 1000
    assert achado.estimation.estimated_saving == pytest.approx(
        round(bruto - transicao, 2), abs=0.01
    )
    assert any("custo de transição" in item for item in achado.estimation.assumptions)


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
    esperado = delta * 300 - 0.01 * 3000 / 1000
    assert achado.estimation.estimated_saving == pytest.approx(round(esperado, 2), abs=0.01)


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


def test_a_truncated_listing_says_the_volume_is_a_floor():
    conta = _conta(
        lido_em="2026-01-01T00:00:00+00:00",
        prefixos=[_prefixo(listing_complete=False)],
    )

    achado = storage_class.detect(conta, _config(), "scan")[0]

    assert any("piso" in item for item in achado.missing_evidence)
    assert any("parcial" in risco for risco in achado.risks)
