"""Detectores de objetos S3 sem consumidor, e o que o tamanho não decide."""

from __future__ import annotations

from julius.collection.models import Account, S3Bucket, S3MultipartUpload, S3Prefix
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal

_DOC_DELETE = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjects.html"
_DOC_MULTIPART = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html"
)
_DOC_STORAGE_CLASS = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html"
)
_DOC_VERSIONING = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html"
)
_DOC_ATHENA_RESULTS = (
    "https://docs.aws.amazon.com/athena/latest/ug/querying.html"
)

_GB = 1024**3

#: O prefixo é avaliado pela regra do seu tipo; o limiar de idade vem daí.
_STALE_THRESHOLD = {
    "athena_results": "s3_athena_results_stale_days",
    "spark_logs": "s3_spark_logs_stale_days",
    "staging": "s3_staging_stale_days",
}

_RULE_BY_KIND = {
    "athena_results": (
        "S3-ATHENA-RESULTS-STALE",
        "Resultados de query Athena acumulados sem consumidor",
        "Apagar os resultados de query além da janela de reutilização",
        _DOC_ATHENA_RESULTS,
    ),
    "spark_logs": (
        "S3-SPARK-LOGS-STALE",
        "Event logs de Spark acumulados além do prazo de utilidade",
        "Apagar os event logs anteriores à janela de análise",
        _DOC_DELETE,
    ),
    "staging": (
        "S3-JOB-STAGING-LEFTOVER",
        "Staging de execução encerrada deixado para trás",
        "Apagar os diretórios de staging de execuções já encerradas",
        _DOC_DELETE,
    ),
}


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    if getattr(account, "s3_mode", "proposal") == "evidence_only":
        return []
    out: list[Opportunity] = []
    custo_por_byte = _storage_cost_per_byte(account)
    for prefixo in getattr(account, "s3_prefixes", []):
        if prefixo.kind not in _RULE_BY_KIND:
            continue
        if not _tem_lixo(prefixo, config):
            continue
        out.append(_stale(account, prefixo, config, scan_id, custo_por_byte))
    for upload in getattr(account, "s3_multipart", []):
        if _multipart_relevante(upload, config):
            out.append(_multipart(account, upload, config, scan_id, custo_por_byte))
    return out


def _tem_lixo(prefixo: S3Prefix, config: Config) -> bool:
    """Objetos antigos o bastante, em quantidade que justifique a ação.

    `stale_object_count is None` significa prefixo não listado — e aí não há o
    que afirmar. Zero significa listado e limpo, que é resposta.
    """
    if prefixo.stale_object_count is None:
        return False
    return prefixo.stale_object_count >= config.thresholds.s3_min_stale_objects


def _multipart_relevante(upload: S3MultipartUpload, config: Config) -> bool:
    return (
        upload.upload_count > 0
        and upload.oldest_age_days is not None
        and upload.oldest_age_days >= config.thresholds.s3_multipart_stale_days
    )


def _storage_cost_per_byte(account: Account) -> float | None:
    """USD por byte na janela, a partir da cobrança rateada.

    Sem cobrança classificada não há tarifa implícita, e nenhum achado de S3
    quantifica economia — a tabela de preço não tem tarifa de S3 de propósito,
    porque o número precisa vir da fatura.
    """
    buckets: list[S3Bucket] = list(getattr(account, "s3_buckets", []) or [])
    custo = sum(item.allocated_storage_cost or 0.0 for item in buckets)
    bytes_totais = sum(item.total_bytes for item in buckets)
    if custo <= 0 or bytes_totais <= 0:
        return None
    return custo / bytes_totais


def _sem_cobranca(method: str, motivo: str) -> Estimation:
    return Estimation(
        method=method,
        baseline_cost=0.0,
        projected_cost=0.0,
        estimated_saving=0.0,
        assumptions=[motivo, "economia não quantificada sem cobrança rateada"],
        saving_quality="unavailable",
    )


def _stale(
    account: Account,
    prefixo: S3Prefix,
    config: Config,
    scan_id: str,
    custo_por_byte: float | None,
) -> Opportunity:
    rule_id, titulo, acao, doc = _RULE_BY_KIND[prefixo.kind]
    dias = getattr(config.thresholds, _STALE_THRESHOLD[prefixo.kind])
    bytes_lixo = prefixo.stale_bytes or 0
    est = _estimation_por_bytes(
        rule_id.lower().replace("-", "_"), bytes_lixo, custo_por_byte
    )
    parcial = not prefixo.listing_complete
    opportunity = build(
        Finding(
            rule_id=rule_id,
            rule_version="1.0.0",
            asset_type="s3_prefix",
            asset_name=prefixo.location,
            title=titulo,
            why=(
                f"{prefixo.stale_object_count} objetos com mais de {dias} dia(s) "
                f"somam {bytes_lixo / _GB:.1f} GB sob um prefixo cujo conteúdo "
                "deixa de ter consumidor depois desse prazo."
            ),
            source_process=prefixo.source_asset or None,
        ),
        Recommendation(
            difficulty=1,
            action=acao,
            # O Julius nunca apaga: a instrução é para quem opera a conta.
            how_to_apply=(
                "Confirmar com o dono do processo que o prazo vale para este "
                "caso e remover os objetos anteriores a ele. O Julius não "
                "executa exclusão — a ação é do time responsável."
            ),
            how_to_validate=(
                "Comparar BucketSizeBytes e a linha de armazenamento do S3 no "
                "Cost Explorer na janela seguinte."
            ),
            risks=[
                "confirmar que nenhum processo relê estes objetos",
                *(["listagem parcial: o volume real pode ser maior"] if parcial else []),
            ],
            docs=[doc, _DOC_DELETE],
            blocked=custo_por_byte is None,
        ),
        Evidence(
            items=[
                f"{prefixo.object_count} objetos no prefixo",
                f"{prefixo.stale_object_count} com mais de {dias} dia(s)",
                f"{bytes_lixo / _GB:.2f} GB elegíveis a exclusão",
                (
                    "listagem completa"
                    if prefixo.listing_complete
                    else "listagem truncada: evidência parcial"
                ),
            ],
            sources=["S3 ListObjectsV2", "CloudWatch AWS/S3", "Cost Explorer"],
            observed_runs=1,
            coverage_days=config.thresholds.min_coverage_days,
            has_optional_metrics=custo_por_byte is not None,
            owner_tag=prefixo.owner_tag,
        ),
        est,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    if parcial:
        opportunity.missing_evidence = [
            "listagem completa do prefixo: o volume medido é piso, não total",
        ]
    return opportunity


def _multipart(
    account: Account,
    upload: S3MultipartUpload,
    config: Config,
    scan_id: str,
    custo_por_byte: float | None,
) -> Opportunity:
    medido = upload.total_bytes is not None
    est = (
        _estimation_por_bytes(
            "s3_incomplete_multipart", upload.total_bytes or 0, custo_por_byte
        )
        if medido
        else _sem_cobranca(
            "s3_incomplete_multipart", "tamanho das partes não consultado"
        )
    )
    opportunity = build(
        Finding(
            rule_id="S3-INCOMPLETE-MULTIPART",
            rule_version="1.0.0",
            asset_type="s3_bucket",
            asset_name=upload.bucket,
            title="Uploads multipart iniciados e nunca concluídos",
            why=(
                f"{upload.upload_count} upload(s) pendente(s), o mais antigo há "
                f"{upload.oldest_age_days} dias. As partes já enviadas são "
                "cobradas como armazenamento e não aparecem em nenhuma listagem "
                "de objetos."
            ),
        ),
        Recommendation(
            difficulty=1,
            action="Abortar os uploads multipart pendentes",
            how_to_apply=(
                "Confirmar que nenhum processo de carga está em andamento e "
                "abortar os uploads anteriores ao prazo. O Julius não executa "
                "a operação — a ação é do time responsável."
            ),
            how_to_validate=(
                "Conferir que ListMultipartUploads volta vazio e comparar "
                "BucketSizeBytes na janela seguinte."
            ),
            risks=["upload em andamento de uma carga longa seria interrompido"],
            docs=[_DOC_MULTIPART],
            blocked=custo_por_byte is None or not medido,
        ),
        Evidence(
            items=[
                f"{upload.upload_count} uploads pendentes",
                f"mais antigo há {upload.oldest_age_days} dias",
                (
                    f"{(upload.total_bytes or 0) / _GB:.2f} GB em partes enviadas"
                    if medido
                    else "tamanho das partes não consultado"
                ),
            ],
            sources=["S3 ListMultipartUploads", "S3 ListParts", "Cost Explorer"],
            observed_runs=1,
            coverage_days=config.thresholds.min_coverage_days,
            has_optional_metrics=medido and custo_por_byte is not None,
            owner_tag=None,
        ),
        est,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    if not medido:
        opportunity.missing_evidence = [
            "soma das partes enviadas (ListParts) para quantificar o volume",
        ]
    return opportunity


def _estimation_por_bytes(
    method: str, bytes_lixo: int, custo_por_byte: float | None
) -> Estimation:
    """Bytes medidos × tarifa implícita na cobrança rateada.

    Apagar objeto zera o armazenamento dele: o contrafactual não é uma fração
    estimada, é zero. Por isso `saving_quality` é `measured` quando existe
    cobrança rateada — o número não sai de faixa de regra, sai da fatura.
    """
    if custo_por_byte is None or bytes_lixo <= 0:
        return _sem_cobranca(method, "cobrança de armazenamento não rateada")
    custo = bytes_lixo * custo_por_byte
    return Estimation(
        method=f"{method}_v1",
        baseline_cost=round(custo, 2),
        projected_cost=0.0,
        estimated_saving=round(custo, 2),
        assumptions=[
            "armazenamento rateado da cobrança real do Cost Explorer na janela",
            "objeto apagado deixa de ser cobrado integralmente",
        ],
        baseline_quality="allocated",
        saving_quality="measured",
    )


def signals(account: Account, config: Config) -> list[Signal]:
    """O que o tamanho mostra e não decide."""
    out: list[Signal] = []
    if getattr(account, "s3_mode", "proposal") == "evidence_only":
        for prefixo in getattr(account, "s3_prefixes", []):
            if prefixo.kind in _RULE_BY_KIND and _tem_lixo(prefixo, config):
                rule_id, _, _, doc = _RULE_BY_KIND[prefixo.kind]
                out.append(
                    Signal(
                        kind="inventory_integrity",
                        rule_id=rule_id,
                        asset_type="s3_prefix",
                        asset_name=prefixo.location,
                        observation=(
                            f"{prefixo.stale_object_count} objetos antigos somam "
                            f"{(prefixo.stale_bytes or 0) / _GB:.2f} GB."
                        ),
                        question=(
                            "Qual processo produtor deixou este resíduo e como "
                            "corrigir seu encerramento ou retenção na origem?"
                        ),
                        missing_evidence=["processo produtor e política de retenção"],
                        doc_links=[doc],
                    )
                )
        for upload in getattr(account, "s3_multipart", []):
            if _multipart_relevante(upload, config):
                out.append(
                    Signal(
                        kind="inventory_integrity",
                        rule_id="S3-INCOMPLETE-MULTIPART",
                        asset_type="s3_bucket",
                        asset_name=upload.bucket,
                        observation=(
                            f"{upload.upload_count} uploads multipart permanecem "
                            f"abertos; o mais antigo há {upload.oldest_age_days} dias."
                        ),
                        question="Qual produtor não finaliza corretamente seus uploads?",
                        missing_evidence=["processo produtor do upload"],
                        doc_links=[_DOC_MULTIPART],
                    )
                )
    for bucket in getattr(account, "s3_buckets", []):
        if bucket.versioning_enabled:
            out.append(
                Signal(
                    kind="config",
                    rule_id="S3-NONCURRENT-VERSIONS",
                    asset_type="s3_bucket",
                    asset_name=bucket.name,
                    observation=(
                        f"'{bucket.name}' tem versionamento ativo e "
                        f"{bucket.total_bytes / _GB:.1f} GB armazenados."
                    ),
                    question=(
                        "As versões não-correntes existem por exigência de "
                        "retenção ou por inércia? Há prazo regulatório que "
                        "impeça apagá-las?"
                    ),
                    missing_evidence=[
                        "política de retenção acordada para este bucket",
                        "volume ocupado por versões não-correntes",
                    ],
                    doc_links=[_DOC_VERSIONING],
                )
            )
    return out


# `S3-COLD-DATA-REWRITE` era emitido aqui, por bucket, a partir do
# `BucketSizeBytes` do CloudWatch. Ele apontava bytes em Standard e parava aí:
# sem saber se o dado é lido, sem tarifa para comparar classes e sem separar
# prefixo — e o CloudWatch não separa —, nunca teve como virar economia.
#
# Agora ele vive em `knowledge/rules/s3/storage_class.py`, por prefixo, ao lado
# da regra que o promove a oportunidade quando a evidência de leitura existe. O
# `rule_id` foi preservado de propósito: ele é parte do fingerprint do achado, e
# trocá-lo desconectaria do backlog as decisões humanas já tomadas sobre ele.
