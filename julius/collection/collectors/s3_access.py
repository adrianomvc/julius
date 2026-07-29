"""Evidência agregada de leitura a partir de S3 Server Access Logs existentes.

Somente logs já configurados são lidos. A coleta é limitada e não persiste
chave, IP, requester, user-agent ou linha bruta: cada registro vira apenas
contagem, bytes e data máxima por prefixo conhecido.
"""

from __future__ import annotations

import gzip
import re
from datetime import datetime
from urllib.parse import unquote

from julius.collection.collectors.paginate import error_category
from julius.collection.collectors.s3_evidence import as_utc, list_objects
from julius.collection.models import S3BucketConfig, S3Prefix
from julius.collection.window import AnalysisWindow

MAX_LOG_OBJECTS = 250
MAX_LOG_BYTES = 10 * 1024**2
_READ_OPERATIONS = frozenset(
    {"REST.GET.OBJECT", "REST.HEAD.OBJECT", "S3.SELECT.OBJECT"}
)
_LOG = re.compile(
    r'^\S+\s+(?P<bucket>\S+)\s+\[(?P<time>[^\]]+)\]\s+'
    r'\S+\s+\S+\s+\S+\s+(?P<operation>\S+)\s+(?P<key>\S+)\s+'
    r'"[^"]*"\s+(?P<status>\S+)\s+\S+\s+(?P<bytes>\S+)'
)


def collect_access_evidence(
    s3_client,
    *,
    prefixes: list[S3Prefix],
    configs: list[S3BucketConfig],
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[str]:
    """Enriquece `prefixes` e devolve as locations efetivamente consultadas."""
    by_target: dict[tuple[str, str], list[str]] = {}
    for config in configs:
        if not config.access_logging_enabled or not config.access_log_target_bucket:
            continue
        by_target.setdefault(
            (config.access_log_target_bucket, config.access_log_target_prefix), []
        ).append(config.bucket)

    measured: set[str] = set()
    for (target_bucket, target_prefix), source_buckets in by_target.items():
        objects, complete = list_objects(
            s3_client,
            target_bucket,
            target_prefix,
            max_pages=1,
            modified_after=window.start,
        )
        bounded = objects[:MAX_LOG_OBJECTS]
        if len(objects) > MAX_LOG_OBJECTS:
            complete = False
        candidates = [
            prefix for prefix in prefixes if prefix.bucket in source_buckets
        ]
        if not candidates:
            continue
        successful_objects = 0
        for item in bounded:
            key = str(item.get("Key") or "")
            try:
                response = s3_client.get_object(Bucket=target_bucket, Key=key)
                payload = response["Body"].read(MAX_LOG_BYTES + 1)
            except Exception as exc:
                _gap(gaps, "get_object(access_log)", error_category(exc))
                complete = False
                continue
            if len(payload) > MAX_LOG_BYTES:
                complete = False
                payload = payload[:MAX_LOG_BYTES]
            if key.endswith(".gz") or response.get("ContentEncoding") == "gzip":
                try:
                    payload = gzip.decompress(payload)
                except (OSError, EOFError):
                    complete = False
                    continue
            successful_objects += 1
            _consume(payload, candidates, window)

        # Só zero inicializado após ao menos um objeto de log lido com sucesso.
        # Uma listagem vazia best-effort não prova ausência de acesso.
        if successful_objects:
            for prefix in candidates:
                if prefix.read_requests_window is None:
                    prefix.read_requests_window = 0
                    prefix.bytes_read_window = 0
                prefix.read_coverage_days = window.days
                prefix.access_source = "server_access_logs"
                prefix.access_quality = "best_effort" if complete else "partial"
                prefix.inventory_data_through = window.end.date().isoformat()
                measured.add(prefix.location)
    return sorted(measured)


def _consume(
    payload: bytes, prefixes: list[S3Prefix], window: AnalysisWindow
) -> None:
    for raw in payload.decode("utf-8", errors="replace").splitlines():
        record = parse_access_log_line(raw)
        if record is None:
            continue
        bucket, key, when, size = record
        if when < window.start or when >= window.end:
            continue
        for prefix in prefixes:
            normalized = prefix.prefix.lstrip("/")
            if prefix.bucket != bucket or not key.startswith(normalized):
                continue
            prefix.read_requests_window = (prefix.read_requests_window or 0) + 1
            prefix.bytes_read_window = (prefix.bytes_read_window or 0) + size
            instant = when.isoformat()
            if instant > prefix.last_read_at:
                prefix.last_read_at = instant


def parse_access_log_line(
    line: str,
) -> tuple[str, str, datetime, int] | None:
    """Extrai somente os quatro campos necessários de uma linha oficial."""
    match = _LOG.match(line)
    if match is None or match.group("operation") not in _READ_OPERATIONS:
        return None
    try:
        status = int(match.group("status"))
    except ValueError:
        return None
    if status < 200 or status >= 400:
        return None
    try:
        when = datetime.strptime(
            match.group("time"), "%d/%b/%Y:%H:%M:%S %z"
        )
    except ValueError:
        return None
    raw_bytes = match.group("bytes")
    size = int(raw_bytes) if raw_bytes.isdigit() else 0
    key = unquote(match.group("key"))
    return match.group("bucket"), key, as_utc(when) or when, size


def _gap(gaps: list[str] | None, operation: str, category: str) -> None:
    if gaps is None:
        return
    value = f"{operation}: {category}"
    if value not in gaps:
        gaps.append(value)
