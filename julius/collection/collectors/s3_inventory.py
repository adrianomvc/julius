"""Consumo read-only de S3 Inventory já configurado pelo dono da conta.

O adapter é uma aceleração opt-in. Ele não cria configuração, não espera a
próxima entrega e não transforma manifesto ausente ou incompatível em zero:
cada alvo não comprovadamente coberto volta para ``ListObjectsV2``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import unquote_plus

from julius.collection.collectors.s3 import _cutoff, _PrefixAggregate
from julius.collection.collectors.s3_evidence import parse_location
from julius.collection.health.recorder import error_category
from julius.collection.models import S3Prefix
from julius.collection.window import AnalysisWindow

KnownPrefix = tuple[str, str, str]
_REQUIRED_FIELDS = frozenset(
    {"Bucket", "Key", "Size", "LastModifiedDate", "StorageClass"}
)
_MAX_CONFIG_PAGES = 20
_MAX_MANIFEST_PAGES = 20


def collect_prefixes(
    s3_client: Any,
    *,
    known: list[KnownPrefix],
    window: AnalysisWindow,
    stale_after_days: int,
    gaps: list[str],
) -> tuple[list[S3Prefix], list[KnownPrefix]]:
    """Resolve alvos por Inventory e devolve os que precisam de fallback.

    O retorno preserva a ordem de ``known``. Um alvo só sai de ``remaining``
    depois que manifesto, schema, data de corte e todos os arquivos foram
    validados e lidos integralmente.
    """
    targets = [
        (item, parsed[0], parsed[1])
        for item in known
        if (parsed := parse_location(item[0])) is not None
    ]
    resolved: dict[KnownPrefix, S3Prefix] = {}
    by_bucket: dict[str, list[tuple[KnownPrefix, str]]] = {}
    for item, bucket, prefix in targets:
        by_bucket.setdefault(bucket, []).append((item, prefix))

    for bucket, bucket_targets in by_bucket.items():
        configs = _configurations(s3_client, bucket, gaps)
        for config in configs:
            pending = [target for target in bucket_targets if target[0] not in resolved]
            covered = _covered_targets(config, pending)
            if not covered:
                continue
            try:
                manifest = _latest_manifest(s3_client, bucket, config, window)
                entries = _consume_manifest(
                    s3_client,
                    bucket,
                    config,
                    manifest,
                    covered,
                    cutoff=_cutoff(window, stale_after_days),
                    window=window,
                )
            except Exception as exc:
                gaps.append(
                    f"s3_inventory[{bucket}/{config.get('Id', '')}]: "
                    f"{error_category(exc)}"
                )
                continue
            resolved.update(entries)

    return (
        [resolved[item] for item in known if item in resolved],
        [item for item in known if item not in resolved],
    )


def _configurations(client: Any, bucket: str, gaps: list[str]) -> list[dict]:
    configs: list[dict] = []
    token = ""
    for _ in range(_MAX_CONFIG_PAGES):
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            response = client.list_bucket_inventory_configurations(**kwargs)
        except Exception as exc:
            gaps.append(
                f"list_bucket_inventory_configurations[{bucket}]: "
                f"{error_category(exc)}"
            )
            return []
        configs.extend(response.get("InventoryConfigurationList", []) or [])
        token = str(response.get("NextContinuationToken") or "")
        if not response.get("IsTruncated") or not token:
            break
    else:
        gaps.append(
            f"list_bucket_inventory_configurations[{bucket}]: bounded_or_incomplete"
        )
    return [
        config
        for config in configs
        if config.get("IsEnabled")
        and str(config.get("Destination", {}).get("S3BucketDestination", {}).get("Format", ""))
        == "CSV"
    ]


def _covered_targets(
    config: dict, targets: list[tuple[KnownPrefix, str]]
) -> list[tuple[KnownPrefix, str]]:
    configured_prefix = str(config.get("Filter", {}).get("Prefix") or "")
    # Um Inventory filtrado em ``raw/2026`` cobre um alvo abaixo dele, mas não
    # prova que ``raw/`` inteiro foi inventariado.
    return [target for target in targets if target[1].startswith(configured_prefix)]


def _latest_manifest(
    client: Any, source_bucket: str, config: dict, window: AnalysisWindow
) -> dict:
    destination = config.get("Destination", {}).get("S3BucketDestination", {})
    destination_bucket = _bucket_from_arn(str(destination.get("Bucket") or ""))
    if not destination_bucket:
        raise ValueError("inventory destination bucket ausente")
    destination_prefix = str(destination.get("Prefix") or "").strip("/")
    root = "/".join(
        part
        for part in (destination_prefix, source_bucket, str(config.get("Id") or ""))
        if part
    ) + "/"
    max_age = 10 if str(config.get("Schedule", {}).get("Frequency")) == "Weekly" else 3
    threshold = window.end - timedelta(days=max_age)
    start_after = root + threshold.strftime("%Y-%m-%dT00-00Z")
    keys: list[str] = []
    token = ""
    for _ in range(_MAX_MANIFEST_PAGES):
        kwargs: dict[str, Any] = {
            "Bucket": destination_bucket,
            "Prefix": root,
            "MaxKeys": 1000,
        }
        if token:
            kwargs["ContinuationToken"] = token
        else:
            kwargs["StartAfter"] = start_after
        response = client.list_objects_v2(**kwargs)
        keys.extend(
            str(item.get("Key"))
            for item in response.get("Contents", []) or []
            if str(item.get("Key") or "").endswith("/manifest.json")
        )
        token = str(response.get("NextContinuationToken") or "")
        if not response.get("IsTruncated") or not token:
            break
    else:
        raise ValueError("manifest listing incompleto")
    if not keys:
        raise ValueError("manifest recente ausente")
    key = max(keys)
    payload = _read_all(client.get_object(Bucket=destination_bucket, Key=key)["Body"])
    manifest = json.loads(payload)
    manifest["_destination_bucket"] = destination_bucket
    return manifest


def _consume_manifest(
    client: Any,
    source_bucket: str,
    config: dict,
    manifest: dict,
    targets: list[tuple[KnownPrefix, str]],
    *,
    cutoff: datetime,
    window: AnalysisWindow,
) -> dict[KnownPrefix, S3Prefix]:
    manifest_source = _bucket_from_arn(str(manifest.get("sourceBucket") or ""))
    if manifest_source != source_bucket:
        raise ValueError("manifest source bucket divergente")
    manifest_destination = _bucket_from_arn(
        str(manifest.get("destinationBucket") or "")
    )
    if manifest_destination != str(manifest["_destination_bucket"]):
        raise ValueError("manifest destination bucket divergente")
    if str(manifest.get("fileFormat") or "") != "CSV":
        raise ValueError("inventory format não suportado")
    created = _manifest_time(manifest.get("creationTimestamp"))
    max_age = 10 if str(config.get("Schedule", {}).get("Frequency")) == "Weekly" else 3
    if created > window.end + timedelta(days=1) or created < window.end - timedelta(days=max_age):
        raise ValueError("manifest fora da janela aceita")

    fields = [field.strip() for field in str(manifest.get("fileSchema") or "").split(",")]
    if not _REQUIRED_FIELDS.issubset(fields):
        raise ValueError("inventory schema incompleto")
    included_versions = str(config.get("IncludedObjectVersions") or "Current")
    if included_versions == "All" and not {"IsLatest", "DeleteMarker"}.issubset(fields):
        raise ValueError("inventory de versões sem marcadores")

    aggregates = {
        item: _PrefixAggregate(source_bucket, prefix, item[1], item[2], cutoff)
        for item, prefix in targets
    }
    trie = _PrefixTrie((prefix, item) for item, prefix in targets)
    destination_bucket = str(manifest["_destination_bucket"])
    files = manifest.get("files", []) or []
    if not files:
        raise ValueError("manifest sem arquivos")
    for file in files:
        key = str(file.get("key") or "")
        if not key:
            raise ValueError("inventory file sem key")
        response = client.get_object(Bucket=destination_bucket, Key=key)
        _consume_csv(
            response["Body"],
            fields,
            source_bucket,
            included_versions,
            trie,
            aggregates,
            expected_md5=str(file.get("MD5checksum") or ""),
        )

    data_through = created.date().isoformat()
    out: dict[KnownPrefix, S3Prefix] = {}
    for item, aggregate in aggregates.items():
        aggregate.observe_page()
        entry = aggregate.finish(complete=True, requests=0)
        entry.inventory_data_through = data_through
        out[item] = entry
    return out


def _consume_csv(
    body: Any,
    fields: list[str],
    source_bucket: str,
    included_versions: str,
    trie: _PrefixTrie,
    aggregates: dict[KnownPrefix, _PrefixAggregate],
    *,
    expected_md5: str,
) -> None:
    raw = _HashingReader(body)
    with gzip.GzipFile(fileobj=cast(Any, raw), mode="rb") as compressed:
        text = io.TextIOWrapper(compressed, encoding="utf-8", newline="")
        for values in csv.reader(text):
            if len(values) != len(fields):
                raise ValueError("inventory row incompatível com schema")
            row = dict(zip(fields, values, strict=True))
            if row["Bucket"] != source_bucket:
                raise ValueError("inventory row de outro bucket")
            if included_versions == "All" and (
                row.get("IsLatest", "").upper() != "TRUE"
                or row.get("DeleteMarker", "").upper() == "TRUE"
            ):
                continue
            key = unquote_plus(row["Key"])
            item = {
                "Key": key,
                "Size": int(row["Size"] or 0),
                "LastModified": _parse_time(row["LastModifiedDate"]),
                "StorageClass": row["StorageClass"] or "STANDARD",
            }
            for target in trie.matches(key):
                aggregates[target].add(item)
    if expected_md5 and raw.hexdigest().lower() != expected_md5.lower():
        raise ValueError("inventory checksum divergente")


class _PrefixTrie:
    """Índice por caractere: custo proporcional à chave, não a alvos × objetos."""

    def __init__(self, prefixes: Iterable[tuple[str, KnownPrefix]]) -> None:
        self.root: dict[str, Any] = {}
        for prefix, target in prefixes:
            node = self.root
            for char in prefix:
                node = node.setdefault(char, {})
            node.setdefault("", []).append(target)

    def matches(self, key: str) -> list[KnownPrefix]:
        node = self.root
        matches: list[KnownPrefix] = list(node.get("", []))
        for char in key:
            child = node.get(char)
            if child is None:
                break
            node = child
            matches.extend(node.get("", []))
        return matches


class _HashingReader:
    def __init__(self, body: Any) -> None:
        self.body = body
        self.digest = hashlib.md5(usedforsecurity=False)

    def read(self, size: int = -1) -> bytes:
        chunk = self.body.read(size)
        if chunk:
            self.digest.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def _read_all(body: Any) -> bytes:
    return body.read()


def _bucket_from_arn(value: str) -> str:
    if value.startswith("arn:") and ":s3:::" in value:
        return value.split(":s3:::", 1)[1].strip("/")
    return value.removeprefix("s3://").strip("/").split("/", 1)[0]


def _manifest_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    if str(value).isdigit():
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    return _parse_time(str(value))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
