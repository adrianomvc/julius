"""O S3 não tem last access time nativo — e o relatório precisa dizer isso.

`LastModified` é a data da última **escrita**. Um arquivo gravado uma vez e lido
todo dia tem `LastModified` de um ano atrás, e mandá-lo para o Glacier a partir
disso troca a classe de dado quente. Saber se um objeto é lido depende de algo
que precisa estar ligado antes, no bucket: server access logs, Storage Lens
advanced, Storage Class Analysis ou Intelligent-Tiering.

Esta fonte não coleta uso; coleta a **capacidade de medir uso**. É o que separa
"estes 4 TB não são lidos" de "estes 4 TB não são regravados, e não temos como
saber se são lidos".
"""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from julius.collection.collectors.s3_config import (
    buckets_without_access_evidence,
    collect_bucket_configs,
)
from julius.collection.models import S3BucketConfig


def _s3():
    return boto3.client(
        "s3",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _negar(stub, operacao, codigo="AccessDenied"):
    stub.add_client_error(
        operacao,
        service_error_code=codigo,
        service_message="not authorized",
        http_status_code=403,
    )


def _bucket_sem_nada(stub, bucket="lake"):
    """Um bucket consultado com sucesso e sem nenhuma configuração."""
    stub.add_response("get_bucket_logging", {}, {"Bucket": bucket})
    stub.add_response(
        "list_bucket_analytics_configurations", {}, {"Bucket": bucket}
    )
    stub.add_response(
        "list_bucket_intelligent_tiering_configurations", {}, {"Bucket": bucket}
    )
    stub.add_client_error(
        "get_bucket_lifecycle_configuration",
        service_error_code="NoSuchLifecycleConfiguration",
        http_status_code=404,
    )
    stub.add_client_error(
        "get_bucket_metadata_configuration",
        service_error_code="MetadataConfigurationNotFoundError",
        http_status_code=404,
    )


# ---------------------------------------------------------------------------
# O veredito: dá ou não para medir último acesso
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "esperado"),
    [
        (S3BucketConfig(bucket="b", access_logging_enabled=True), "server_access_logs"),
        (S3BucketConfig(bucket="b", storage_lens_enabled=True), "storage_lens"),
        (
            S3BucketConfig(bucket="b", storage_class_analysis_ids=["tudo"]),
            "storage_class_analysis",
        ),
        (
            S3BucketConfig(bucket="b", intelligent_tiering_ids=["auto"]),
            "intelligent_tiering",
        ),
        # Consultado e nada ligado: é o caso comum, e o que limita a análise.
        (S3BucketConfig(bucket="b", access_logging_enabled=False), "none"),
        # Nem consultado: também é "none", e o `gaps` da coleta diz por quê.
        (S3BucketConfig(bucket="b"), "none"),
    ],
)
def test_the_bucket_says_which_access_evidence_it_allows(config, esperado):
    assert config.last_access_source == esperado


def test_the_most_precise_source_wins():
    """Server access logs é por objeto; Intelligent-Tiering é agregado."""
    config = S3BucketConfig(
        bucket="b", access_logging_enabled=True, intelligent_tiering_ids=["auto"]
    )
    assert config.last_access_source == "server_access_logs"


# ---------------------------------------------------------------------------
# Onde já há automação, recomendar transição é cobrar duas vezes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        S3BucketConfig(bucket="b", intelligent_tiering_ids=["auto"]),
        S3BucketConfig(
            bucket="b",
            lifecycle_rules=[{"ID": "frio", "Transitions": [{"Days": 90}]}],
        ),
        S3BucketConfig(
            bucket="b",
            lifecycle_rules=[
                {"ID": "versoes", "NoncurrentVersionTransitions": [{"NoncurrentDays": 30}]}
            ],
        ),
    ],
)
def test_a_bucket_that_already_transitions_is_recognized(config):
    assert config.transitions_automatically is True


@pytest.mark.parametrize(
    "config",
    [
        S3BucketConfig(bucket="b", lifecycle_rules=[]),
        # Expiração apaga; não move de classe. A recomendação continua valendo.
        S3BucketConfig(
            bucket="b", lifecycle_rules=[{"ID": "apaga", "Expiration": {"Days": 30}}]
        ),
        # Não consultado não é "não tem".
        S3BucketConfig(bucket="b"),
    ],
)
def test_a_bucket_without_transition_automation_is_not_suppressed(config):
    assert config.transitions_automatically is False


# ---------------------------------------------------------------------------
# A coleta
# ---------------------------------------------------------------------------


def test_it_reads_what_is_enabled_without_touching_a_single_object():
    s3 = _s3()
    with Stubber(s3) as stub:
        stub.add_response(
            "get_bucket_logging",
            {"LoggingEnabled": {"TargetBucket": "logs", "TargetPrefix": "lake/"}},
            {"Bucket": "lake"},
        )
        stub.add_response(
            "list_bucket_analytics_configurations",
            {"AnalyticsConfigurationList": [{"Id": "tudo", "StorageClassAnalysis": {}}]},
            {"Bucket": "lake"},
        )
        stub.add_response(
            "list_bucket_intelligent_tiering_configurations",
            {},
            {"Bucket": "lake"},
        )
        stub.add_response(
            "get_bucket_lifecycle_configuration",
            {"Rules": [{"ID": "apaga", "Status": "Enabled", "Prefix": ""}]},
            {"Bucket": "lake"},
        )
        stub.add_response(
            "get_bucket_metadata_configuration",
            {
                "GetBucketMetadataConfigurationResult": {
                    "MetadataConfigurationResult": {
                        "DestinationResult": {
                            "TableBucketType": "aws",
                            "TableBucketArn": "arn:aws:s3tables:sa-east-1:123456789012:bucket/aws-s3",
                            "TableNamespace": "lake",
                        }
                    }
                }
            },
            {"Bucket": "lake"},
        )

        configs = collect_bucket_configs(s3, names=["lake"])

    config = configs[0]
    assert config.access_logging_enabled is True
    assert config.storage_class_analysis_ids == ["tudo"]
    assert config.intelligent_tiering_ids == []
    assert [regra["ID"] for regra in config.lifecycle_rules or []] == ["apaga"]
    assert config.metadata_table_enabled is True
    assert config.last_access_source == "server_access_logs"


def test_metadata_tables_do_not_count_as_access_evidence():
    """Elas substituem a listagem; não dizem quem leu.

    A journal table registra CREATE, UPDATE_METADATA e DELETE — mutação. Um
    `GetObject` não gera linha nenhuma, então ter S3 Metadata ligado não permite
    afirmar que um objeto deixou de ser lido.
    """
    config = S3BucketConfig(bucket="b", metadata_table_enabled=True)

    assert config.last_access_source == "none"


def test_no_metadata_configuration_is_an_answer_too():
    s3 = _s3()
    with Stubber(s3) as stub:
        _bucket_sem_nada(stub)
        configs = collect_bucket_configs(s3, names=["lake"])

    assert configs[0].metadata_table_enabled is False


def test_no_lifecycle_is_an_answer_not_a_failure():
    """`NoSuchLifecycleConfiguration` autoriza afirmar que não há automação.

    Se virasse `None`, a regra não poderia distinguir "não tem lifecycle" de
    "não consegui olhar" — e a supressão por automação existente ficaria cega.
    """
    s3 = _s3()
    with Stubber(s3) as stub:
        _bucket_sem_nada(stub)
        configs = collect_bucket_configs(s3, names=["lake"])

    assert configs[0].lifecycle_rules == []
    assert configs[0].transitions_automatically is False


def test_a_denied_call_stays_unmeasured_and_never_becomes_false():
    """`False` se lê como "consultado e desligado" — e isso seria mentira."""
    s3 = _s3()
    with Stubber(s3) as stub:
        _negar(stub, "get_bucket_logging")
        _negar(stub, "list_bucket_analytics_configurations")
        _negar(stub, "list_bucket_intelligent_tiering_configurations")
        _negar(stub, "get_bucket_lifecycle_configuration")
        _negar(stub, "get_bucket_metadata_configuration")

        gaps: list[str] = []
        configs = collect_bucket_configs(s3, names=["lake"], gaps=gaps)

    config = configs[0]
    assert config.access_logging_enabled is None
    assert config.storage_class_analysis_ids is None
    assert config.lifecycle_rules is None
    assert "get_bucket_logging: permission_denied" in gaps


def test_a_denied_bucket_does_not_erase_the_other_buckets():
    s3 = _s3()
    with Stubber(s3) as stub:
        _negar(stub, "get_bucket_logging")
        _negar(stub, "list_bucket_analytics_configurations")
        _negar(stub, "list_bucket_intelligent_tiering_configurations")
        _negar(stub, "get_bucket_lifecycle_configuration")
        _negar(stub, "get_bucket_metadata_configuration")
        _bucket_sem_nada(stub, "visivel")

        configs = collect_bucket_configs(s3, names=["negado", "visivel"])

    assert [config.bucket for config in configs] == ["negado", "visivel"]
    assert configs[0].lifecycle_rules is None
    assert configs[1].lifecycle_rules == []


def test_the_same_missing_permission_is_reported_once_not_once_per_bucket():
    """Cem buckets negados são uma permissão faltando, não cem problemas."""
    s3 = _s3()
    with Stubber(s3) as stub:
        for _ in range(3):
            _negar(stub, "get_bucket_logging")
            _negar(stub, "list_bucket_analytics_configurations")
            _negar(stub, "list_bucket_intelligent_tiering_configurations")
            _negar(stub, "get_bucket_lifecycle_configuration")
            _negar(stub, "get_bucket_metadata_configuration")

        gaps: list[str] = []
        collect_bucket_configs(s3, names=["a", "b", "c"], gaps=gaps)

    assert gaps.count("get_bucket_logging: permission_denied") == 1


def test_the_buckets_that_cannot_prove_a_read_are_named():
    configs = [
        S3BucketConfig(bucket="cego"),
        S3BucketConfig(bucket="medido", access_logging_enabled=True),
        S3BucketConfig(bucket="tambem-cego", access_logging_enabled=False),
    ]

    assert buckets_without_access_evidence(configs) == ["cego", "tambem-cego"]
