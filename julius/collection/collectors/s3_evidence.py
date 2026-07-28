"""Leitura de S3 compartilhada: listagem limitada e agregação de tamanhos.

Antes existiam duas implementações separadas de "ler objetos de um prefixo S3" —
uma dentro do coletor Athena, para arquivos pequenos e compressão, e outra no
coletor de Spark event logs. Elas divergiam nos limites e no tratamento de
`LastModified`, e nenhuma das duas sabia da existência da outra.

Nada aqui persiste chave, conteúdo ou metadado de objeto: a saída é agregada.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

_MB = 1024**2

# Um prefixo de event log pode ter milhares de objetos. Os limites são
# explícitos para que evidência truncada nunca vire zero silencioso — quem
# chama recebe se a listagem foi completa.
MAX_LIST_PAGES = 5
SMALL_FILE_THRESHOLD_BYTES = 64 * _MB
SMALL_FILE_MIN_COUNT = 100

COMPRESSED_SUFFIXES = (
    ".gz", ".gzip", ".bz2", ".bzip2", ".snappy", ".zst", ".zstd", ".lz4"
)

_EMPTY: dict[str, Any] = {
    "count": 0,
    "average": 0,
    "total": 0,
    "small_files": False,
    "compressed_count": 0,
    "complete": False,
}


def parse_location(value: str | None) -> tuple[str, str] | None:
    """`s3://bucket/prefixo` → `(bucket, prefixo/)`, ou `None` se não for S3."""
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    prefix = parsed.path.lstrip("/")
    if not prefix:
        return None
    return parsed.netloc, prefix.rstrip("/") + "/"


def as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def list_objects(
    s3_client,
    bucket: str,
    prefix: str,
    *,
    modified_after: datetime | None = None,
    max_objects: int | None = None,
    max_pages: int = MAX_LIST_PAGES,
) -> tuple[list[dict], bool]:
    """Objetos do prefixo, mais recentes primeiro, e se a listagem foi completa.

    `modified_after` descarta o que é anterior à janela. O segundo retorno é
    falso quando a paginação foi cortada ou quando `max_objects` truncou —
    quem chama precisa saber que a evidência é parcial.
    """
    candidates: list[dict] = []
    token = None
    listing_complete = True
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            response = s3_client.list_objects_v2(**kwargs)
        except Exception:
            return [], False
        for item in response.get("Contents", []):
            modified = as_utc(item.get("LastModified"))
            if modified_after is not None and modified is not None:
                if modified < modified_after:
                    continue
            candidates.append(item)
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated") or not token:
            break
    else:
        listing_complete = False

    candidates.sort(
        key=lambda item: as_utc(item.get("LastModified"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if max_objects is not None and len(candidates) > max_objects:
        return candidates[:max_objects], False
    return candidates, listing_complete


def object_evidence(
    s3_client, location: str | None, *, max_pages: int = MAX_LIST_PAGES
) -> dict[str, Any]:
    """Agrega tamanhos e extensões de um prefixo; nunca devolve chaves.

    Esta função paginava sem teto, no mesmo arquivo cuja razão de existir é
    "listagem limitada" — e é chamada uma vez por tabela lida por query. Num
    prefixo de data lake com milhões de objetos isso são milhares de chamadas
    para responder uma pergunta que se decide nas primeiras centenas.

    `complete` diz se a listagem terminou. Os dois vereditos que dependem daqui
    se decidem muito antes do teto — `SMALL_FILE_MIN_COUNT` são 100 objetos, e o
    teto são cinco páginas de mil — então truncar não muda conclusão; muda
    quanto se paga para chegar nela.
    """
    parsed = parse_location(location)
    if s3_client is None or parsed is None:
        return dict(_EMPTY)
    bucket, prefix = parsed

    sizes: list[int] = []
    compressed_count = 0
    complete = True
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
        for index, page in enumerate(pages, start=1):
            for obj in page.get("Contents", []):
                size = int(obj.get("Size") or 0)
                if size <= 0:
                    continue
                sizes.append(size)
                key = str(obj.get("Key") or "").lower()
                compressed_count += key.endswith(COMPRESSED_SUFFIXES)
            # O corte é aqui, e não no topo do laço: perguntar ao paginador se
            # há mais é uma chamada a mais. Um prefixo que acaba exatamente no
            # teto é reportado como incompleto — erra para o lado de "pode haver
            # mais", que é o lado honesto.
            if index >= max_pages:
                complete = False
                break
    except Exception:
        return dict(_EMPTY)

    if not sizes:
        return {**_EMPTY, "complete": complete}
    average = round(sum(sizes) / len(sizes))
    return {
        "count": len(sizes),
        "average": average,
        "total": sum(sizes),
        "small_files": (
            len(sizes) >= SMALL_FILE_MIN_COUNT
            and average < SMALL_FILE_THRESHOLD_BYTES
        ),
        "compressed_count": compressed_count,
        "complete": complete,
    }
