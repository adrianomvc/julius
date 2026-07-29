from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

from julius.collection.collectors.s3 import _agregar
from julius.collection.collectors.s3_access import (
    collect_access_evidence,
    parse_access_log_line,
)
from julius.collection.models import S3BucketConfig, S3Prefix
from julius.collection.window import AnalysisWindow

_LINE = (
    "owner lake [10/Jul/2026:12:00:00 +0000] 10.0.0.1 requester req "
    'REST.GET.OBJECT vendas/a.parquet "GET /lake/vendas/a.parquet HTTP/1.1" '
    "200 - 2048 2048 10 9 - agent - host sig cipher auth host TLS - -"
)


def test_server_access_log_parser_returns_no_identity_or_raw_key_container():
    parsed = parse_access_log_line(_LINE)

    assert parsed is not None
    bucket, key, when, size = parsed
    assert (bucket, key, size) == ("lake", "vendas/a.parquet", 2048)
    assert when.isoformat() == "2026-07-10T12:00:00+00:00"


def test_existing_access_logs_enrich_only_aggregated_prefix_fields():
    window = AnalysisWindow(
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 29, tzinfo=timezone.utc),
        days=28,
    )

    class Client:
        def list_objects_v2(self, **_kwargs):
            return {
                "Contents": [
                    {
                        "Key": "logs/one",
                        "LastModified": datetime(
                            2026, 7, 11, tzinfo=timezone.utc
                        ),
                    }
                ]
            }

        def get_object(self, **_kwargs):
            return {"Body": BytesIO((_LINE + "\n").encode())}

    prefix = S3Prefix(bucket="lake", prefix="vendas/")
    measured = collect_access_evidence(
        Client(),
        prefixes=[prefix],
        configs=[
            S3BucketConfig(
                bucket="lake",
                access_logging_enabled=True,
                access_log_target_bucket="audit",
                access_log_target_prefix="logs/",
            )
        ],
        window=window,
    )

    assert measured == ["s3://lake/vendas/"]
    assert prefix.read_requests_window == 1
    assert prefix.bytes_read_window == 2048
    assert prefix.last_read_at == "2026-07-10T12:00:00+00:00"
    assert prefix.access_quality == "best_effort"
    assert not hasattr(prefix, "object_keys")


def test_access_logs_split_get_head_and_select_requests():
    window = AnalysisWindow(
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 29, tzinfo=timezone.utc),
        days=28,
    )
    head = _LINE.replace("REST.GET.OBJECT", "REST.HEAD.OBJECT").replace(
        '"GET /lake/', '"HEAD /lake/'
    )
    select = _LINE.replace("REST.GET.OBJECT", "S3.SELECT.OBJECT")

    class Client:
        def list_objects_v2(self, **_kwargs):
            return {
                "Contents": [
                    {
                        "Key": "logs/one",
                        "LastModified": datetime(
                            2026, 7, 11, tzinfo=timezone.utc
                        ),
                    }
                ]
            }

        def get_object(self, **_kwargs):
            payload = "\n".join((_LINE, head, select))
            return {"Body": BytesIO(payload.encode())}

    prefix = S3Prefix(bucket="lake", prefix="vendas/")
    collect_access_evidence(
        Client(),
        prefixes=[prefix],
        configs=[
            S3BucketConfig(
                bucket="lake",
                access_logging_enabled=True,
                access_log_target_bucket="audit",
                access_log_target_prefix="logs/",
            )
        ],
        window=window,
    )

    assert prefix.read_requests_window == 3
    assert prefix.get_requests_window == 1
    assert prefix.head_requests_window == 1
    assert prefix.select_requests_window == 1


def test_prefix_aggregation_excludes_zero_markers_from_average():
    now = datetime.now(timezone.utc)
    prefix = _agregar(
        "lake",
        "vendas/",
        "table_location",
        "db.vendas",
        [
            {"Key": "vendas/", "Size": 0, "LastModified": now},
            {
                "Key": "vendas/a",
                "Size": 256 * 1024,
                "StorageClass": "STANDARD",
                "LastModified": now,
            },
        ],
        True,
        datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    assert prefix.object_count == 2
    assert prefix.nonzero_object_count == 1
    assert prefix.average_object_bytes == 256 * 1024
    assert prefix.object_count_by_size == {"zero": 1, "128kb-1mb": 1}
    assert prefix.object_count_by_age == {"0-30": 2}
    assert prefix.object_count_by_class_size["STANDARD"]["zero"] == 1
