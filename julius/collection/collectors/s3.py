"""Coleta read-only de S3: panorama por métrica, detalhe por prefixo conhecido.

**O Julius lê e recomenda; quem apaga é o time dono.** Nada aqui escreve,
apaga ou aborta coisa alguma — as operações usadas são `GetMetricStatistics`,
`ListObjectsV2`, `ListMultipartUploads` e `GetBucketVersioning`.

O desenho existe por causa de um risco concreto: listar um data lake cobra por
request, e uma coleta ingênua custaria dinheiro para descobrir custo. Então o
tamanho vem do CloudWatch, que é diário e gratuito, e a listagem fica restrita
aos caminhos que o inventário já conhece — resultado de query, event log,
location de tabela. Prefixo fora desses caminhos não é procurado, e o relatório
diz isso em vez de deixar parecer que o bucket foi varrido.

Evidência truncada nunca vira zero: `listing_complete` acompanha cada prefixo, e
métrica não consultada deixa o campo em `None`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from julius.collection.collectors.s3_evidence import as_utc, list_objects, parse_location
from julius.collection.models import S3Bucket, S3MultipartUpload, S3Prefix
from julius.collection.window import AnalysisWindow

#: Sufixos que denunciam staging de execução Spark/Hadoop, não dado.
STAGING_MARKERS = ("_temporary/", ".spark-staging", "_$folder$", ".hive-staging")

#: Uploads examinados por bucket. `ListMultipartUploads` pagina, e um bucket com
#: milhares de uploads pendentes não pode transformar a coleta em varredura.
MAX_MULTIPART_PAGES = 3


def collect_buckets(
    cloudwatch_client,
    s3_client=None,
    *,
    names: list[str],
    window: AnalysisWindow,
) -> list[S3Bucket]:
    """Tamanho e composição por classe, sem listar um objeto.

    `BucketSizeBytes` é publicado por `StorageType` uma vez ao dia e não custa
    nada. É a única forma de saber o tamanho de um data lake sem pagar por isso.
    """
    out: list[S3Bucket] = []
    for name in names:
        bucket = S3Bucket(name=name, coverage_days=window.days)
        if cloudwatch_client is not None:
            bucket.bytes_by_class = _size_by_class(cloudwatch_client, name, window)
            bucket.object_count = _object_count(cloudwatch_client, name, window)
            bucket.observed_days = 1 if bucket.bytes_by_class else 0
        bucket.versioning_enabled = _versioning(s3_client, name)
        out.append(bucket)
    return out


def collect_prefixes(
    s3_client,
    *,
    known: list[tuple[str, str, str]],
    window: AnalysisWindow,
    stale_after_days: int,
) -> list[S3Prefix]:
    """Agrega cada prefixo conhecido: quantos objetos, quanto, quão antigos.

    `known` é uma lista de `(location, kind, source_asset)` montada por quem
    conhece o inventário — a coleta não descobre prefixo sozinha, de propósito.
    """
    cutoff = _cutoff(window, stale_after_days)
    out: list[S3Prefix] = []
    for location, kind, source in known:
        parsed = parse_location(location)
        if s3_client is None or parsed is None:
            continue
        bucket, prefix = parsed
        objects, complete = list_objects(s3_client, bucket, prefix)
        entry = S3Prefix(
            bucket=bucket,
            prefix=prefix,
            kind=kind,
            source_asset=source,
            listing_complete=complete,
        )
        if not objects and not complete:
            # Listagem falhou: sem contagem, e nenhuma regra vai afirmar.
            out.append(entry)
            continue
        entry.object_count = len(objects)
        entry.total_bytes = sum(int(item.get("Size") or 0) for item in objects)
        idades = [
            idade for item in objects if (idade := _age_days(item)) is not None
        ]
        entry.oldest_object_age_days = max(idades) if idades else None
        antigos = [
            item
            for item in objects
            if (modified := as_utc(item.get("LastModified"))) is not None
            and modified < cutoff
        ]
        entry.stale_object_count = len(antigos)
        entry.stale_bytes = sum(int(item.get("Size") or 0) for item in antigos)
        out.append(entry)
    return out


def collect_multipart_uploads(
    s3_client, *, names: list[str], window: AnalysisWindow
) -> list[S3MultipartUpload]:
    """Uploads iniciados e nunca concluídos.

    Cobram armazenamento pelas partes enviadas e não aparecem em nenhuma
    listagem de objetos — é o desperdício que uma inspeção pelo console não
    encontra. O tamanho exige `ListParts` por upload; sem ele o campo fica
    `None` e a regra diz que o volume não foi medido.
    """
    out: list[S3MultipartUpload] = []
    for name in names:
        if s3_client is None:
            continue
        uploads, complete = _multipart(s3_client, name)
        entry = S3MultipartUpload(bucket=name, listing_complete=complete)
        if uploads is None:
            out.append(entry)
            continue
        entry.upload_count = len(uploads)
        idades = [_age_days(item, key="Initiated") for item in uploads]
        medidas = [value for value in idades if value is not None]
        entry.oldest_age_days = max(medidas) if medidas else None
        entry.total_bytes = _multipart_bytes(s3_client, name, uploads)
        out.append(entry)
    return out


def _multipart(s3_client, bucket: str) -> tuple[list[dict] | None, bool]:
    uploads: list[dict] = []
    key_marker = upload_marker = None
    for _ in range(MAX_MULTIPART_PAGES):
        kwargs: dict[str, Any] = {"Bucket": bucket, "MaxUploads": 1000}
        if key_marker:
            kwargs["KeyMarker"] = key_marker
            kwargs["UploadIdMarker"] = upload_marker
        try:
            response = s3_client.list_multipart_uploads(**kwargs)
        except Exception:
            return None, False
        uploads.extend(response.get("Uploads", []) or [])
        if not response.get("IsTruncated"):
            return uploads, True
        key_marker = response.get("NextKeyMarker")
        upload_marker = response.get("NextUploadIdMarker")
    return uploads, False


def _multipart_bytes(s3_client, bucket: str, uploads: list[dict]) -> int | None:
    """Soma as partes já enviadas; `None` quando nem uma consulta funcionou."""
    total = 0
    consultados = 0
    for upload in uploads:
        key, upload_id = upload.get("Key"), upload.get("UploadId")
        if not key or not upload_id:
            continue
        try:
            response = s3_client.list_parts(
                Bucket=bucket, Key=key, UploadId=upload_id
            )
        except Exception:
            continue
        consultados += 1
        total += sum(int(part.get("Size") or 0) for part in response.get("Parts", []))
    return total if consultados else None


def _size_by_class(cloudwatch_client, bucket: str, window) -> dict[str, float]:
    tamanhos: dict[str, float] = {}
    for storage_type in (
        "StandardStorage",
        "StandardIAStorage",
        "OneZoneIAStorage",
        "IntelligentTieringFAStorage",
        "IntelligentTieringIAStorage",
        "GlacierInstantRetrievalStorage",
        "GlacierStorage",
        "DeepArchiveStorage",
        "ReducedRedundancyStorage",
    ):
        pontos = _metric(
            cloudwatch_client,
            "BucketSizeBytes",
            [
                {"Name": "BucketName", "Value": bucket},
                {"Name": "StorageType", "Value": storage_type},
            ],
            window,
        )
        if pontos:
            tamanhos[storage_type] = round(pontos[-1], 2)
    return tamanhos


def _object_count(cloudwatch_client, bucket: str, window) -> int | None:
    pontos = _metric(
        cloudwatch_client,
        "NumberOfObjects",
        [
            {"Name": "BucketName", "Value": bucket},
            {"Name": "StorageType", "Value": "AllStorageTypes"},
        ],
        window,
    )
    return int(pontos[-1]) if pontos else None


def _metric(cloudwatch_client, metric: str, dimensions, window) -> list[float]:
    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=window.start,
            EndTime=window.end,
            Period=86400,
            Statistics=["Average"],
        )
    except Exception:
        return []
    pontos = sorted(
        (item for item in response.get("Datapoints", []) if "Average" in item),
        key=lambda item: item.get("Timestamp") or datetime.min,
    )
    return [float(item["Average"]) for item in pontos]


def _versioning(s3_client, bucket: str) -> bool | None:
    if s3_client is None:
        return None
    try:
        response = s3_client.get_bucket_versioning(Bucket=bucket)
    except Exception:
        return None
    return str(response.get("Status") or "").lower() == "enabled"


def _cutoff(window: AnalysisWindow, days: int) -> datetime:
    from datetime import timedelta

    return (window.end or datetime.now(timezone.utc)) - timedelta(days=days)


def _age_days(item: dict, key: str = "LastModified") -> int | None:
    momento = as_utc(item.get(key))
    if momento is None:
        return None
    return max(0, (datetime.now(timezone.utc) - momento).days)


def staging_prefixes(locations: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Deriva os caminhos de staging a partir das locations de tabela."""
    return [
        (f"{location.rstrip('/')}/{marker.strip('/')}", "staging", asset)
        for location, asset in locations
        for marker in STAGING_MARKERS
        if location
    ]
