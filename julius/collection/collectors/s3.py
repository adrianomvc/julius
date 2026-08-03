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

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from julius.collection.collectors import metrics
from julius.collection.collectors.metrics import MetricQuery
from julius.collection.collectors.s3_evidence import (
    MAX_LIST_PAGES,
    as_utc,
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

    # Só a listagem completa pode compartilhar um pai. Com teto, as primeiras
    # páginas do pai podem terminar antes da faixa lexicográfica de um filho;
    # reaproveitá-las faria o filho parecer vazio. No full, cada chave do pai é
    # vista e pode alimentar todos os filhos sem ser retida em memória.
    groups = (
        _overlapping_groups(alvos)
        if max_pages is None
        else [[(index, alvo)] for index, alvo in enumerate(alvos)]
    )

    def coletar(group: list[tuple[int, tuple[str, str, str, str]]]):
        return _stream_group(s3_client, group, cutoff, max_pages=max_pages)

    if workers <= 1 or len(groups) == 1:
        resolved = [coletar(group) for group in groups]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as pool:
            resolved = list(pool.map(coletar, groups))
    # Ordem do inventário, não ordem dos grupos ou das respostas.
    by_index = {index: item for group in resolved for index, item in group}
    return [by_index[index] for index in range(len(alvos))]


def _overlapping_groups(
    targets: list[tuple[str, str, str, str]],
) -> list[list[tuple[int, tuple[str, str, str, str]]]]:
    """Agrupa filhos sob o menor prefixo pai já autorizado no inventário."""
    groups: list[list[tuple[int, tuple[str, str, str, str]]]] = []
    ordered = sorted(
        enumerate(targets), key=lambda item: (item[1][0], len(item[1][1]), item[1][1])
    )
    for indexed in ordered:
        _index, (bucket, prefix, _kind, _source) = indexed
        parent = next(
            (
                group
                for group in groups
                if group[0][1][0] == bucket and prefix.startswith(group[0][1][1])
            ),
            None,
        )
        if parent is None:
            groups.append([indexed])
        else:
            parent.append(indexed)
    return groups


def _stream_group(
    s3_client,
    group: list[tuple[int, tuple[str, str, str, str]]],
    cutoff: datetime,
    *,
    max_pages: int | None,
) -> list[tuple[int, S3Prefix]]:
    if len(group) == 1:
        index, (bucket, prefix, kind, source) = group[0]
        return [(
            index,
            _stream_prefix(
                s3_client,
                bucket,
                prefix,
                kind,
                source,
                cutoff,
                max_pages=max_pages,
            ),
        )]

    root_index, (bucket, root_prefix, root_kind, root_source) = group[0]
    aggregates = [
        (
            index,
            target_prefix,
            _PrefixAggregate(
                target_bucket, target_prefix, target_kind, target_source, cutoff
            ),
        )
        for index, (target_bucket, target_prefix, target_kind, target_source) in group
    ]
    token = None
    requests = 0
    complete = True
    while True:
        requests += 1
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": root_prefix,
            "MaxKeys": 1000,
        }
        if token:
            kwargs["ContinuationToken"] = token
        try:
            response = s3_client.list_objects_v2(**kwargs)
        except Exception:
            complete = False
            break
        for item in response.get("Contents", []) or []:
            key = str(item.get("Key") or "")
            for _index, target_prefix, aggregate in aggregates:
                if key.startswith(target_prefix):
                    aggregate.add(item)
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated") or not token:
            break

    if not complete:
        # O pai parcial não prova que um filho sem item observado esteja vazio.
        # Releitura individual preserva a cobertura que existia antes do merge.
        root = aggregates[0][2].finish(complete=False, requests=requests)
        out = [(root_index, root)]
        for index, (_target_bucket, prefix, kind, source) in group[1:]:
            out.append((
                index,
                _stream_prefix(
                    s3_client,
                    bucket,
                    prefix,
                    kind,
                    source,
                    cutoff,
                    max_pages=max_pages,
                ),
            ))
        return out

    out = []
    for position, (index, _prefix, aggregate) in enumerate(aggregates):
        aggregate.observe_page()
        # Apenas o pai pagou requests; filhos foram derivados no mesmo stream.
        out.append((index, aggregate.finish(complete=True, requests=requests if position == 0 else 0)))
    return out


#: Chaves de partição temporal, no estilo Hive. Cobre português porque o
#: catálogo destas contas usa os dois.
_PARTICAO_TEMPORAL = re.compile(
    r"(?:^|/)(dt|ds|data|date|ano|year|mes|month|dia|day|hora|hour)=",
    re.IGNORECASE,
)


def _particionado_por_data(objects: list[dict]) -> bool:
    """A fonte cresce por partição de data, ou é reescrita inteira?

    A distinção decide se reler tudo a cada execução é desperdício ou
    necessidade — e é a condição que o cálculo de reprocessamento exige antes de
    afirmar qualquer byte redundante.

    Calculado aqui porque é aqui que as chaves existem: elas não sobem deste
    módulo, por decisão de privacidade, então o que sobe é o booleano.
    """
    return any(
        _PARTICAO_TEMPORAL.search(str(item.get("Key") or "")) for item in objects
    )


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
    aggregate = _PrefixAggregate(bucket, prefix, kind, source, cutoff)
    aggregate.observe_page()
    for item in objects:
        aggregate.add(item)
    requests = max(1, -(-len(objects) // 1000)) if objects else 1
    return aggregate.finish(complete=complete, requests=requests)


def _stream_prefix(
    s3_client,
    bucket: str,
    prefix: str,
    kind: str,
    source: str,
    cutoff: datetime,
    *,
    max_pages: int | None,
) -> S3Prefix:
    """Agrega página a página; memória não cresce com o total de objetos."""
    aggregate = _PrefixAggregate(bucket, prefix, kind, source, cutoff)
    token = None
    requests = 0
    complete = True
    page_number = 0
    while max_pages is None or page_number < max_pages:
        page_number += 1
        requests += 1
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            response = s3_client.list_objects_v2(**kwargs)
        except Exception:
            complete = False
            break
        aggregate.observe_page()
        for item in response.get("Contents", []) or []:
            aggregate.add(item)
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated") or not token:
            break
    else:
        complete = False
    return aggregate.finish(complete=complete, requests=requests)


class _PrefixAggregate:
    """Acumulador sem chaves: retém apenas contagens e somas normalizadas."""

    def __init__(
        self,
        bucket: str,
        prefix: str,
        kind: str,
        source: str,
        cutoff: datetime,
    ) -> None:
        self.entry = S3Prefix(
            bucket=bucket,
            prefix=prefix,
            kind=kind,
            source_asset=source,
        )
        # Inicialização explícita mantém o contrato "campo medido tem escritor"
        # verificável por AST, além de garantir um acumulador novo por prefixo.
        self.entry.bytes_by_size = {}
        self.entry.object_count_by_size = {}
        self.entry.bytes_by_class_size = {}
        self.entry.object_count_by_class_size = {}
        self.cutoff = cutoff
        self.observed = False

    def observe_page(self) -> None:
        if self.observed:
            return
        self.observed = True
        self.entry.object_count = 0
        self.entry.total_bytes = 0
        self.entry.nonzero_object_count = 0
        self.entry.stale_object_count = 0
        self.entry.stale_bytes = 0

    def add(self, item: dict) -> None:
        self.observe_page()
        entry = self.entry
        size = int(item.get("Size") or 0)
        entry.object_count = int(entry.object_count or 0) + 1
        entry.total_bytes = int(entry.total_bytes or 0) + size
        if size > 0:
            entry.nonzero_object_count = int(entry.nonzero_object_count or 0) + 1
        key = str(item.get("Key") or "")
        entry.date_partitioned = entry.date_partitioned or bool(
            _PARTICAO_TEMPORAL.search(key)
        )
        age = _age_days(item)
        if age is not None:
            entry.oldest_object_age_days = max(
                entry.oldest_object_age_days or age, age
            )
            age_range = age_bucket(age)
            entry.bytes_by_age[age_range] = (
                entry.bytes_by_age.get(age_range, 0.0) + size
            )
            entry.object_count_by_age[age_range] = (
                entry.object_count_by_age.get(age_range, 0) + 1
            )
        modified = as_utc(item.get("LastModified"))
        if modified is not None and modified < self.cutoff:
            entry.stale_object_count = int(entry.stale_object_count or 0) + 1
            entry.stale_bytes = int(entry.stale_bytes or 0) + size

        storage_class = str(item.get("StorageClass") or "STANDARD")
        entry.bytes_by_class[storage_class] = (
            entry.bytes_by_class.get(storage_class, 0.0) + size
        )
        entry.object_count_by_class[storage_class] = (
            entry.object_count_by_class.get(storage_class, 0) + 1
        )
        size_range = size_bucket(size)
        entry.bytes_by_size[size_range] = entry.bytes_by_size.get(size_range, 0.0) + size
        entry.object_count_by_size[size_range] = (
            entry.object_count_by_size.get(size_range, 0) + 1
        )
        class_bytes = entry.bytes_by_class_size.setdefault(storage_class, {})
        class_bytes[size_range] = class_bytes.get(size_range, 0.0) + size
        class_count = entry.object_count_by_class_size.setdefault(storage_class, {})
        class_count[size_range] = class_count.get(size_range, 0) + 1

    def finish(self, *, complete: bool, requests: int) -> S3Prefix:
        entry = self.entry
        entry.listing_complete = complete
        entry.list_requests = max(0, requests)
        if self.observed and entry.nonzero_object_count:
            entry.average_object_bytes = round(
                int(entry.total_bytes or 0) / entry.nonzero_object_count
            )
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
