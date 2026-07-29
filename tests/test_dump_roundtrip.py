"""O dataset gravado pela coleta ao vivo tem que conter tudo que foi coletado.

Este arquivo existe por causa de um sumiço silencioso: `account_to_dataset` não
serializava `s3_buckets`, `s3_prefixes`, `s3_multipart`, `s3_cost_coverage`,
`redshift_clusters` nem `redshift_cost_coverage`. O loader sabia lê-los e o
dataset de exemplo os continha, então tudo passava nos testes — mas o
`julius collect` grava por essa função, e na conta real a coleta inteira de S3 e
de Redshift era jogada fora entre coletar e reportar. Nenhuma regra desses dois
serviços disparava, e o relatório não tinha como dizer por quê: para ele o
inventário simplesmente estava vazio.

O teste é escrito contra o `Account`, não contra uma lista de chaves: se alguém
acrescentar uma coleção nova ao inventário e esquecer do dump, é aqui que falha.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, fields

from julius.collection.models import (
    Account,
    ActorEvent,
    AthenaActorUsage,
    AthenaCoverage,
    AthenaQuery,
    CollectionHealth,
    DataBrewJob,
    GlueCostCoverage,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    InteractiveSession,
    PreviousResult,
    ProcessCost,
    ProducerCandidate,
    RedshiftCluster,
    RedshiftCostCoverage,
    S3Bucket,
    S3BucketConfig,
    S3CostCoverage,
    S3MultipartUpload,
    S3Prefix,
    SageMakerApp,
    SageMakerCostCoverage,
    SageMakerDomain,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerInferenceRecommendation,
    SageMakerJob,
    SageMakerMonitoringSchedule,
    SageMakerNotebook,
    SageMakerPipeline,
    SageMakerSavingsPlanCoverage,
    SageMakerSpace,
    Schedule,
    ServiceCost,
    StateMachine,
    Table,
)
from julius.collection.normalizers.dump import account_to_dataset
from julius.collection.normalizers.loader import load_account

#: Cada coleção do inventário e a classe que a povoa. O teste abaixo cobra que
#: este mapa cubra o `Account` inteiro — é o que transforma "esqueci do dump" em
#: falha de teste em vez de inventário vazio na conta real.
COLECOES = {
    "collection_health": CollectionHealth,
    "services": ServiceCost,
    "glue_jobs": GlueJob,
    "interactive_sessions": InteractiveSession,
    "glue_crawlers": GlueCrawler,
    "glue_triggers": GlueTrigger,
    "databrew_jobs": DataBrewJob,
    "process_costs": ProcessCost,
    "athena_queries": AthenaQuery,
    "athena_actor_usage": AthenaActorUsage,
    "athena_coverage": AthenaCoverage,
    "glue_cost_coverage": GlueCostCoverage,
    "state_machines": StateMachine,
    "sagemaker_apps": SageMakerApp,
    "sagemaker_spaces": SageMakerSpace,
    "sagemaker_domains": SageMakerDomain,
    "sagemaker_endpoints": SageMakerEndpoint,
    "sagemaker_notebooks": SageMakerNotebook,
    "sagemaker_jobs": SageMakerJob,
    "sagemaker_feature_groups": SageMakerFeatureGroup,
    "sagemaker_pipelines": SageMakerPipeline,
    "sagemaker_monitoring_schedules": SageMakerMonitoringSchedule,
    "sagemaker_inference_recommendations": SageMakerInferenceRecommendation,
    "sagemaker_cost_coverage": SageMakerCostCoverage,
    "sagemaker_savings_plans": SageMakerSavingsPlanCoverage,
    "redshift_clusters": RedshiftCluster,
    "redshift_cost_coverage": RedshiftCostCoverage,
    "s3_buckets": S3Bucket,
    "s3_prefixes": S3Prefix,
    "s3_multipart": S3MultipartUpload,
    "s3_bucket_configs": S3BucketConfig,
    "s3_cost_coverage": S3CostCoverage,
    "tables": Table,
    "schedules": Schedule,
    "actor_events": ActorEvent,
    "producer_candidates": ProducerCandidate,
    "previous_results": PreviousResult,
}


def _valor(anotacao: str):
    """Um valor plausível para o tipo anotado, que sobrevive a JSON."""
    texto = str(anotacao)
    if texto.startswith("list"):
        return []
    if texto.startswith("dict"):
        return {}
    if texto.startswith("bool"):
        return False
    if texto.startswith("float"):
        return 1.0
    if texto.startswith("int"):
        return 1
    return "marcador"


def _instancia(cls):
    """Instancia `cls` preenchendo só o que não tem default."""
    obrigatorios = {
        campo.name: _valor(campo.type)
        for campo in fields(cls)
        if campo.default is MISSING and campo.default_factory is MISSING
    }
    return cls(**obrigatorios)


def _conta_completa() -> Account:
    conta = Account(account_id="123456789012", period="2026-07", generated_at="2026-07-29")
    for nome, cls in COLECOES.items():
        atual = getattr(conta, nome)
        item = _instancia(cls)
        setattr(conta, nome, [item] if isinstance(atual, list) else item)
    return conta


def _roundtrip(conta: Account, tmp_path) -> Account:
    destino = tmp_path / "account.json"
    destino.write_text(
        json.dumps(account_to_dataset(conta), ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return load_account(destino)


def test_the_map_covers_every_collection_in_the_inventory():
    """Coleção nova no `Account` sem entrada aqui falha antes de sumir no dump."""
    # Uma coleção é uma lista (`default_factory=list`) ou uma cobertura opcional
    # (`default=None`). Os demais campos do `Account` são escalares do cabeçalho
    # — conta, região, janela, moeda — e não passam por este mapa.
    colecoes = {
        campo.name
        for campo in fields(Account)
        if campo.default_factory is list or campo.default is None
    }
    assert colecoes == set(COLECOES), (
        f"sem cobertura no dump: {colecoes - set(COLECOES)}; "
        f"no mapa mas não no Account: {set(COLECOES) - colecoes}"
    )


def test_no_collection_is_lost_between_collect_and_report(tmp_path):
    conta = _conta_completa()
    lida = _roundtrip(conta, tmp_path)
    perdidas = [
        nome
        for nome in COLECOES
        if not getattr(lida, nome)
    ]
    assert perdidas == [], f"o dump descartou: {perdidas}"


def test_s3_inventory_survives_the_live_collection(tmp_path):
    """O caso concreto que estava quebrado, escrito por extenso."""
    conta = Account(account_id="123456789012")
    conta.s3_buckets = [S3Bucket(name="lake", bytes_by_class={"StandardStorage": 42.0})]
    conta.s3_prefixes = [S3Prefix(bucket="lake", prefix="tabela/", stale_object_count=7)]
    conta.s3_multipart = [S3MultipartUpload(bucket="lake", upload_count=3)]
    conta.s3_cost_coverage = S3CostCoverage(net_cost=12.5, cost_quality="allocated")

    lida = _roundtrip(conta, tmp_path)

    assert lida.s3_buckets[0].bytes_by_class == {"StandardStorage": 42.0}
    assert lida.s3_prefixes[0].stale_object_count == 7
    assert lida.s3_multipart[0].upload_count == 3
    assert lida.s3_cost_coverage is not None
    assert lida.s3_cost_coverage.net_cost == 12.5


def test_unmeasured_stays_unmeasured_across_the_roundtrip(tmp_path):
    """`None` é "não medido" e não pode virar `0` no caminho de ida e volta.

    `_clean` remove as chaves nulas e o loader repõe o default da dataclass —
    a convenção só se mantém porque os dois lados concordam.
    """
    conta = Account(account_id="123456789012")
    conta.s3_prefixes = [S3Prefix(bucket="lake", prefix="tabela/")]

    lida = _roundtrip(conta, tmp_path)

    prefixo = lida.s3_prefixes[0]
    assert prefixo.object_count is None
    assert prefixo.stale_object_count is None
    assert prefixo.oldest_object_age_days is None
