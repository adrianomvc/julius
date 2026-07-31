"""Coleta read-only de S3: panorama por métrica, detalhe por prefixo conhecido.

**O Julius lê e recomenda; quem apaga é o time dono.** Nada aqui escreve,
apaga ou aborta coisa alguma — as operações usadas são `GetMetricData`,
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

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from julius.collection.collectors import metrics
from julius.collection.collectors.metrics import MetricQuery
from julius.collection.collectors.s3_evidence import (
    MAX_LIST_PAGES,
    as_utc,
    list_objects,
    parse_location,
)
from julius.collection.models import S3Bucket, S3MultipartUpload, S3Prefix
from julius.collection.models.s3 import age_bucket, size_bucket
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
        bucket.versioning_enabled = _versioning(s3_client, name)
        out.append(bucket)
    _enrich_sizes(cloudwatch_client, out, window)
    return out


#: Classes de armazenamento consultadas por bucket. `BucketSizeBytes` é publicado
#: por `StorageType` uma vez ao dia e não custa nada.
_STORAGE_TYPES = (
    "StandardStorage",
    "StandardIAStorage",
    "OneZoneIAStorage",
    "IntelligentTieringFAStorage",
    "IntelligentTieringIAStorage",
    "GlacierInstantRetrievalStorage",
    "GlacierStorage",
    "DeepArchiveStorage",
    "ReducedRedundancyStorage",
)


def _enrich_sizes(cloudwatch_client, buckets: list[S3Bucket], window) -> None:
    """Tamanho por classe e contagem de objetos de todos os buckets, em lote.

    Eram dez chamadas por bucket — nove classes e a contagem — em série. Cinquenta
    buckets pagavam quinhentas latências; as mesmas quinhentas consultas cabem em
    uma chamada.

    `ScanBy` ascendente importa aqui: o tamanho de um bucket é o **último** ponto
    da série, não um agregado dela, e o default do `GetMetricData` é descendente.
    """
    if cloudwatch_client is None or not buckets:
        return
    pedidos = [
        (bucket, storage_type, MetricQuery(
            namespace="AWS/S3",
            metric_name="BucketSizeBytes",
            stat="Average",
            dimensions=(
                ("BucketName", bucket.name),
                ("StorageType", storage_type),
            ),
        ))
        for bucket in buckets
        for storage_type in _STORAGE_TYPES
    ]
    contagens = [
        (bucket, MetricQuery(
            namespace="AWS/S3",
            metric_name="NumberOfObjects",
            stat="Average",
            dimensions=(
                ("BucketName", bucket.name),
                ("StorageType", "AllStorageTypes"),
            ),
        ))
        for bucket in buckets
    ]
    metrics.collect(
        cloudwatch_client,
        [query for _b, _t, query in pedidos] + [query for _b, query in contagens],
        start=window.start,
        end=window.end,
        scan_by="TimestampAscending",
    )

    for bucket, storage_type, query in pedidos:
        if query.values:
            bucket.bytes_by_class[storage_type] = round(query.values[-1], 2)
    for bucket, query in contagens:
        if query.values:
            bucket.object_count = int(query.values[-1])
    for bucket in buckets:
        bucket.observed_days = 1 if bucket.bytes_by_class else 0


def collect_prefixes(
    s3_client,
    *,
    known: list[tuple[str, str, str]],
    window: AnalysisWindow,
    stale_after_days: int,
    max_pages: int | None = MAX_LIST_PAGES,
    workers: int = 1,
) -> list[S3Prefix]:
    """Agrega cada prefixo conhecido: quantos objetos, quanto, quão antigos.

    `known` é uma lista de `(location, kind, source_asset)` montada por quem
    conhece o inventário — a coleta não descobre prefixo sozinha, de propósito.

    `max_pages=None` lista o prefixo inteiro; `workers` lista vários prefixos ao
    mesmo tempo. Os dois andam juntos: sem teto, um data lake leva horas em
    série, e o gargalo é latência de rede, não CPU. Clientes botocore são
    seguros entre threads depois de criados — o que precisa acompanhar é o
    `max_pool_connections` da sessão, senão as threads disputam o pool e o
    paralelismo vira fila.
    """
    cutoff = _cutoff(window, stale_after_days)
    alvos = [
        (parsed[0], parsed[1], kind, source)
        for location, kind, source in known
        if s3_client is not None and (parsed := parse_location(location)) is not None
    ]
    if not alvos:
        return []

    def coletar(alvo: tuple[str, str, str, str]) -> S3Prefix:
        bucket, prefix, kind, source = alvo
        objects, complete = list_objects(
            s3_client, bucket, prefix, max_pages=max_pages
        )
        return _agregar(bucket, prefix, kind, source, objects, complete, cutoff)

    if workers <= 1 or len(alvos) == 1:
        return [coletar(alvo) for alvo in alvos]

    with ThreadPoolExecutor(max_workers=min(workers, len(alvos))) as pool:
        # `map` preserva a ordem da entrada: dois scans do mesmo inventário
        # produzem o mesmo dataset, e o diff entre eles continua legível.
        return list(pool.map(coletar, alvos))


def _agregar(
    bucket: str,
    prefix: str,
    kind: str,
    source: str,
    objects: list[dict],
    complete: bool,
    cutoff: datetime,
) -> S3Prefix:
    """Transforma a listagem em agregado. Nenhuma chave de objeto sobe daqui."""
    entry = S3Prefix(
        bucket=bucket,
        prefix=prefix,
        kind=kind,
        source_asset=source,
        listing_complete=complete,
    )
    entry.list_requests = max(1, -(-len(objects) // 1000)) if objects else 1
    if not objects and not complete:
        # Listagem falhou: sem contagem, e nenhuma regra vai afirmar.
        return entry
    entry.object_count = len(objects)
    entry.total_bytes = sum(int(item.get("Size") or 0) for item in objects)
    entry.nonzero_object_count = sum(
        1 for item in objects if int(item.get("Size") or 0) > 0
    )
    entry.average_object_bytes = (
        round(entry.total_bytes / entry.nonzero_object_count)
        if entry.nonzero_object_count
        else None
    )

    idades = [idade for item in objects if (idade := _age_days(item)) is not None]
    entry.oldest_object_age_days = max(idades) if idades else None

    antigos = [
        item
        for item in objects
        if (modified := as_utc(item.get("LastModified"))) is not None
        and modified < cutoff
    ]
    entry.stale_object_count = len(antigos)
    entry.stale_bytes = sum(int(item.get("Size") or 0) for item in antigos)

    por_classe: dict[str, float] = {}
    contagem_classe: dict[str, int] = {}
    por_idade: dict[str, float] = {}
    contagem_idade: dict[str, int] = {}
    por_tamanho: dict[str, float] = {}
    contagem_tamanho: dict[str, int] = {}
    classe_tamanho: dict[str, dict[str, float]] = {}
    contagem_classe_tamanho: dict[str, dict[str, int]] = {}
    for item in objects:
        tamanho = int(item.get("Size") or 0)
        # `StorageClass` vem junto no `ListObjectsV2` e era descartado; ausente
        # significa STANDARD, que é como a própria API o omite.
        classe = str(item.get("StorageClass") or "STANDARD")
        por_classe[classe] = por_classe.get(classe, 0.0) + tamanho
        contagem_classe[classe] = contagem_classe.get(classe, 0) + 1
        faixa_tamanho = size_bucket(tamanho)
        por_tamanho[faixa_tamanho] = por_tamanho.get(faixa_tamanho, 0.0) + tamanho
        contagem_tamanho[faixa_tamanho] = (
            contagem_tamanho.get(faixa_tamanho, 0) + 1
        )
        classe_tamanho.setdefault(classe, {})[faixa_tamanho] = (
            classe_tamanho.setdefault(classe, {}).get(faixa_tamanho, 0.0)
            + tamanho
        )
        contagem_classe_tamanho.setdefault(classe, {})[faixa_tamanho] = (
            contagem_classe_tamanho.setdefault(classe, {}).get(faixa_tamanho, 0)
            + 1
        )
        if (idade := _age_days(item)) is not None:
            faixa = age_bucket(idade)
            por_idade[faixa] = por_idade.get(faixa, 0.0) + tamanho
            contagem_idade[faixa] = contagem_idade.get(faixa, 0) + 1
    entry.bytes_by_class = por_classe
    entry.object_count_by_class = contagem_classe
    entry.bytes_by_age = por_idade
    entry.object_count_by_age = contagem_idade
    entry.bytes_by_size = por_tamanho
    entry.object_count_by_size = contagem_tamanho
    entry.bytes_by_class_size = classe_tamanho
    entry.object_count_by_class_size = contagem_classe_tamanho
    return entry


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


def known_prefixes(account: Any) -> list[tuple[str, str, str]]:
    """Os prefixos que o inventário já conhece, como `(location, kind, ativo)`.

    O escopo é derivado, nunca descoberto: cada caminho aqui veio de um recurso
    que outra fonte já coletou — a location da tabela no catálogo, o
    `--spark-event-logs-path` do job, a saída de resultados do workgroup. É o
    que permite inventariar S3 sem `ListBuckets` e sem varrer bucket atrás de
    prefixo, que custaria dinheiro para descobrir custo.
    """
    out: list[tuple[str, str, str]] = []
    table_locations: list[tuple[str, str]] = []
    for table in getattr(account, "tables", []) or []:
        location = str(getattr(table, "location", "") or "")
        if not location:
            continue
        out.append((location, "table_location", table.name))
        table_locations.append((location, table.name))
    for job in getattr(account, "glue_jobs", []) or []:
        path = str(getattr(job, "spark_event_logs_path", "") or "")
        if path:
            out.append((path, "spark_logs", job.name))
    coverage = getattr(account, "athena_coverage", None)
    for workgroup, location in (
        getattr(coverage, "workgroup_output_locations", {}) or {}
    ).items():
        if location:
            out.append((str(location), "athena_results", workgroup))
    out.extend(staging_prefixes(table_locations))
    # Duas tabelas podem apontar para a mesma location; listar o prefixo duas
    # vezes cobraria dois requests para a mesma resposta.
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for location, kind, asset in out:
        key = f"{kind}|{location.rstrip('/')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append((location, kind, asset))
    return unique


def bucket_names(prefixes: list[tuple[str, str, str]]) -> list[str]:
    """Os buckets que aparecem nos prefixos conhecidos, sem `ListBuckets`.

    Descobrir bucket exigiria `s3:ListAllMyBuckets`, que ampliaria o alcance da
    credencial que o produto pede. O inventário responde a mesma pergunta para
    os buckets que importam: aqueles onde a conta efetivamente tem alguma coisa.
    """
    names: list[str] = []
    for location, _kind, _asset in prefixes:
        parsed = parse_location(location)
        if parsed and parsed[0] not in names:
            names.append(parsed[0])
    return names
