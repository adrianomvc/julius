"""S3: o que se resolve apagando arquivo, e o que o tamanho não decide.

O ambiente não permite alterar infraestrutura de S3 — só criar e apagar
objetos. Isso tira de cena lifecycle e transição de classe por configuração, e
deixa exatamente o desperdício que mais existe num data lake: resultado de query
que ninguém apaga, upload que nunca terminou, staging de execução que falhou.

E o Julius recomenda: não apaga nada.
"""

from __future__ import annotations

import re
from pathlib import Path

from julius.collection.models import (
    Account,
    S3Bucket,
    S3CostCoverage,
    S3MultipartUpload,
    S3Prefix,
)
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules import families_without_evidence, missing_evidence
from julius.knowledge.rules.s3 import rules as s3_rules

RAIZ = Path(__file__).resolve().parents[1] / "julius"


def _conta(**overrides) -> Account:
    """Conta com cobrança rateada: US$ 100 sobre 100 GB."""
    defaults = dict(
        account_id="123456789012",
        s3_buckets=[
            S3Bucket(
                name="lake",
                bytes_by_class={"StandardStorage": 100 * 1024**3},
                object_count=1_000_000,
                observed_days=1,
                coverage_days=30,
                allocated_storage_cost=100.0,
                cost_quality="reconciled",
            )
        ],
        s3_cost_coverage=S3CostCoverage(
            buckets={"storage_standard": 100.0}, cost_quality="reconciled"
        ),
    )
    defaults.update(overrides)
    return Account(**defaults)


def _prefixo(**overrides) -> S3Prefix:
    defaults = dict(
        bucket="lake",
        prefix="athena-results/",
        kind="athena_results",
        object_count=10_000,
        total_bytes=10 * 1024**3,
        oldest_object_age_days=400,
        stale_object_count=9_500,
        stale_bytes=10 * 1024**3,
        listing_complete=True,
        source_asset="workgroup primary",
    )
    defaults.update(overrides)
    return S3Prefix(**defaults)


def test_julius_never_touches_an_object():
    """A fronteira é executável, não uma promessa no texto."""
    mutacoes = re.compile(
        r"\b(delete_object|delete_objects|put_object|abort_multipart_upload|"
        r"upload_file|copy_object|put_bucket_lifecycle\w*)\s*\("
    )
    ofensores = [
        caminho.relative_to(RAIZ).as_posix()
        for caminho in RAIZ.rglob("*.py")
        if mutacoes.search(caminho.read_text(encoding="utf-8"))
    ]

    assert not ofensores, (
        f"o Julius lê S3 e recomenda; quem apaga é o time dono: {ofensores}"
    )


def test_stale_query_results_become_a_finding_anchored_in_billing():
    """Ninguém apaga resultado de Athena, e ele acumula em silêncio."""
    account = _conta(s3_prefixes=[_prefixo()])

    found = s3_rules.detect(account, DEFAULT_CONFIG, "scan")
    achado = next(o for o in found if o.rule_id == "S3-ATHENA-RESULTS-STALE")

    assert achado.blocked is False
    assert achado.estimation is not None
    # 10 GB de 100 GB que custam US$ 100 → US$ 10 de baseline.
    assert achado.estimation.baseline_cost == 10.0
    assert achado.estimation.projected_cost == 0.0
    assert achado.estimation.baseline_quality == "allocated"
    assert achado.evidence_quality == "allocated"


def test_a_clean_prefix_produces_nothing():
    """Listar e não achar lixo é resposta, não achado."""
    account = _conta(
        s3_prefixes=[_prefixo(stale_object_count=0, stale_bytes=0)]
    )

    assert not s3_rules.detect(account, DEFAULT_CONFIG, "scan")


def test_an_unlisted_prefix_is_not_accused():
    """`None` é prefixo não listado — e sobre isso não há o que afirmar."""
    account = _conta(
        s3_prefixes=[
            _prefixo(
                object_count=None,
                stale_object_count=None,
                stale_bytes=None,
                listing_complete=False,
            )
        ]
    )

    assert not s3_rules.detect(account, DEFAULT_CONFIG, "scan")


def test_a_truncated_listing_says_the_volume_is_a_floor():
    account = _conta(s3_prefixes=[_prefixo(listing_complete=False)])

    achado = s3_rules.detect(account, DEFAULT_CONFIG, "scan")[0]

    assert achado.missing_evidence
    assert any("piso" in item for item in achado.missing_evidence)
    assert any("parcial" in item for item in achado.risks)


def test_without_apportioned_billing_the_finding_claims_nothing():
    """Sem cobrança rateada não há tarifa implícita — e a tabela de preço não
    tem tarifa de S3 de propósito."""
    account = _conta(
        s3_buckets=[S3Bucket(name="lake", bytes_by_class={"StandardStorage": 1.0})],
        s3_prefixes=[_prefixo()],
    )

    achado = s3_rules.detect(account, DEFAULT_CONFIG, "scan")[0]

    assert achado.blocked is True
    assert achado.estimated_gain.monthly_expected == 0
    assert achado.estimation is not None
    assert achado.estimation.saving_quality == "unavailable"


def test_an_old_multipart_upload_is_billed_and_invisible():
    """Partes enviadas cobram storage e não aparecem em listagem de objetos."""
    account = _conta(
        s3_multipart=[
            S3MultipartUpload(
                bucket="lake",
                upload_count=40,
                total_bytes=5 * 1024**3,
                oldest_age_days=200,
            )
        ]
    )

    achado = next(
        o
        for o in s3_rules.detect(account, DEFAULT_CONFIG, "scan")
        if o.rule_id == "S3-INCOMPLETE-MULTIPART"
    )

    assert achado.blocked is False
    assert achado.estimation is not None
    assert achado.estimation.baseline_cost == 5.0


def test_a_recent_multipart_upload_may_still_finish():
    account = _conta(
        s3_multipart=[
            S3MultipartUpload(
                bucket="lake", upload_count=3, total_bytes=1024, oldest_age_days=1
            )
        ]
    )

    assert not s3_rules.detect(account, DEFAULT_CONFIG, "scan")


def test_multipart_without_part_sizes_reports_the_gap_instead_of_a_number():
    account = _conta(
        s3_multipart=[
            S3MultipartUpload(
                bucket="lake", upload_count=40, total_bytes=None, oldest_age_days=200
            )
        ]
    )

    achado = s3_rules.detect(account, DEFAULT_CONFIG, "scan")[0]

    assert achado.blocked is True
    assert achado.estimated_gain.monthly_expected == 0
    assert any("ListParts" in item for item in achado.missing_evidence)


def test_the_recommendation_says_who_deletes():
    """A instrução é para quem opera; o Julius não executa exclusão."""
    account = _conta(s3_prefixes=[_prefixo()])

    achado = s3_rules.detect(account, DEFAULT_CONFIG, "scan")[0]

    assert "não executa" in achado.how_to_apply
    assert "time responsável" in achado.how_to_apply


def test_what_the_size_does_not_decide_becomes_a_question():
    account = _conta()
    account.s3_buckets[0].versioning_enabled = True

    sinais = {s.rule_id: s for s in s3_rules.signals(account, DEFAULT_CONFIG)}

    assert "S3-NONCURRENT-VERSIONS" in sinais
    assert "S3-COLD-DATA-REWRITE" in sinais
    for sinal in sinais.values():
        assert sinal.question and sinal.missing_evidence
    # Lifecycle está fora do ambiente, e a pergunta precisa dizer isso.
    assert "fria" in sinais["S3-COLD-DATA-REWRITE"].question


def test_without_cloudwatch_the_family_explains_its_own_silence():
    """Bucket sem métrica não é bucket pequeno."""
    account = Account(account_id="123456789012", s3_buckets=[S3Bucket(name="lake")])

    assert not s3_rules.detect(account, DEFAULT_CONFIG, "scan")

    familia = next(
        f for f in families_without_evidence(account) if f.service == "s3"
    )
    assert "medição ausente" in missing_evidence(account, familia)
