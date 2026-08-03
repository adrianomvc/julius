from __future__ import annotations

import gzip
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from julius.collection import sources
from julius.collection.collectors import s3_inventory
from julius.collection.models import S3Prefix
from julius.collection.window import AnalysisWindow

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)
WINDOW = AnalysisWindow(start=NOW - timedelta(days=30), end=NOW, days=30)
SCHEMA = "Bucket, Key, Size, LastModifiedDate, StorageClass"


class FakeS3:
    def __init__(
        self,
        *,
        checksum: str | None = None,
        source: str = "source",
        created: datetime = NOW,
        schema: str = SCHEMA,
    ) -> None:
        csv_payload = gzip.compress(
            b'source,"data%2Fyear%3D2025%2Fa.parquet",10,2026-01-01T00:00:00Z,STANDARD\n'
            b'source,"data%2Fyear%3D2025%2Fchild%2Fb.parquet",20,2026-07-28T00:00:00Z,STANDARD_IA\n'
        )
        digest = hashlib.md5(csv_payload, usedforsecurity=False).hexdigest()
        manifest = {
            "sourceBucket": f"arn:aws:s3:::{source}",
            "destinationBucket": "arn:aws:s3:::destination",
            "creationTimestamp": int(created.timestamp() * 1000),
            "fileFormat": "CSV",
            "fileSchema": schema,
            "files": [
                {
                    "key": "inventory/source/config/files/part.csv.gz",
                    "MD5checksum": checksum if checksum is not None else digest,
                }
            ],
        }
        self.objects = {
            "inventory/source/config/2026-07-29T00-00Z/manifest.json": json.dumps(
                manifest
            ).encode(),
            "inventory/source/config/files/part.csv.gz": csv_payload,
        }
        self.listed_prefixes: list[str] = []

    def list_bucket_inventory_configurations(self, **kwargs):
        assert kwargs["Bucket"] == "source"
        return {
            "InventoryConfigurationList": [
                {
                    "Id": "config",
                    "IsEnabled": True,
                    "IncludedObjectVersions": "Current",
                    "Destination": {
                        "S3BucketDestination": {
                            "Bucket": "arn:aws:s3:::destination",
                            "Prefix": "inventory",
                            "Format": "CSV",
                        }
                    },
                    "Schedule": {"Frequency": "Daily"},
                }
            ],
            "IsTruncated": False,
        }

    def list_objects_v2(self, **kwargs):
        self.listed_prefixes.append(kwargs["Prefix"])
        return {
            "Contents": [
                {"Key": key} for key in self.objects if key.endswith("manifest.json")
            ],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def test_inventory_agrega_prefixos_pai_e_filho_sem_reter_chaves() -> None:
    client = FakeS3()
    known = [
        ("s3://source/data/", "table_location", "table"),
        ("s3://source/data/year=2025/child/", "table_location", "child"),
    ]
    gaps: list[str] = []

    entries, remaining = s3_inventory.collect_prefixes(
        client,
        known=known,
        window=WINDOW,
        stale_after_days=30,
        gaps=gaps,
    )

    assert remaining == []
    assert gaps == []
    assert [entry.object_count for entry in entries] == [2, 1]
    assert [entry.total_bytes for entry in entries] == [30, 20]
    assert all(entry.listing_complete for entry in entries)
    assert all(entry.list_requests == 0 for entry in entries)
    assert all(entry.inventory_data_through == "2026-07-29" for entry in entries)
    assert all(not hasattr(entry, "keys") for entry in entries)


def test_inventory_invalido_preserva_alvo_para_fallback() -> None:
    gaps: list[str] = []
    known = [("s3://source/data/", "table_location", "table")]

    entries, remaining = s3_inventory.collect_prefixes(
        FakeS3(checksum="invalid"),
        known=known,
        window=WINDOW,
        stale_after_days=30,
        gaps=gaps,
    )

    assert entries == []
    assert remaining == known
    assert gaps == ["s3_inventory[source/config]: invalid_response"]


def test_inventory_de_outro_bucket_nao_e_aceito() -> None:
    gaps: list[str] = []
    known = [("s3://source/data/", "table_location", "table")]

    entries, remaining = s3_inventory.collect_prefixes(
        FakeS3(source="other"),
        known=known,
        window=WINDOW,
        stale_after_days=30,
        gaps=gaps,
    )

    assert entries == []
    assert remaining == known
    assert gaps


def test_inventory_atrasado_nao_substitui_listagem_atual() -> None:
    gaps: list[str] = []
    known = [("s3://source/data/", "table_location", "table")]

    entries, remaining = s3_inventory.collect_prefixes(
        FakeS3(created=NOW - timedelta(days=4)),
        known=known,
        window=WINDOW,
        stale_after_days=30,
        gaps=gaps,
    )

    assert entries == []
    assert remaining == known
    assert gaps == ["s3_inventory[source/config]: invalid_response"]


def test_inventory_sem_campos_obrigatorios_nao_produz_zero() -> None:
    gaps: list[str] = []
    known = [("s3://source/data/", "table_location", "table")]

    entries, remaining = s3_inventory.collect_prefixes(
        FakeS3(schema="Bucket, Key, Size"),
        known=known,
        window=WINDOW,
        stale_after_days=30,
        gaps=gaps,
    )

    assert entries == []
    assert remaining == known
    assert gaps


class DeniedInventoryS3(FakeS3):
    def list_bucket_inventory_configurations(self, **kwargs):
        exc = RuntimeError("denied")
        exc.response = {"Error": {"Code": "AccessDenied"}}  # type: ignore[attr-defined]
        raise exc


def test_inventory_sem_iam_registra_operacao_e_preserva_fallback() -> None:
    gaps: list[str] = []
    known = [("s3://source/data/", "table_location", "table")]

    entries, remaining = s3_inventory.collect_prefixes(
        DeniedInventoryS3(),
        known=known,
        window=WINDOW,
        stale_after_days=30,
        gaps=gaps,
    )

    assert entries == []
    assert remaining == known
    assert gaps == [
        "list_bucket_inventory_configurations[source]: permission_denied"
    ]


def test_fallback_parcial_preserva_ordem_original(monkeypatch) -> None:
    known = [
        ("s3://source/first/", "table_location", "first"),
        ("s3://source/second/", "table_location", "second"),
    ]
    first = S3Prefix(
        bucket="source", prefix="first/", kind="table_location", source_asset="first"
    )
    second = S3Prefix(
        bucket="source", prefix="second/", kind="table_location", source_asset="second"
    )
    monkeypatch.setattr(
        sources.s3_inventory,
        "collect_prefixes",
        lambda *_args, **_kwargs: ([second], [known[0]]),
    )
    monkeypatch.setattr(
        sources.s3,
        "collect_prefixes",
        lambda *_args, **_kwargs: [first],
    )
    thresholds = SimpleNamespace(
        s3_athena_results_stale_days=1,
        s3_spark_logs_stale_days=30,
        s3_staging_stale_days=7,
    )
    ctx = SimpleNamespace(
        config=SimpleNamespace(thresholds=thresholds),
        client=lambda _service: object(),
        flags={"s3_prefixes": known},
        s3_inventory=True,
        s3_full_listing=False,
        window=WINDOW,
        gaps=[],
    )

    result = sources._collect_s3_prefixes(ctx)

    assert [entry.source_asset for entry in result] == ["first", "second"]
