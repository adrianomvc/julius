"""Leitura limitada de Spark event logs para shuffle/spill de Glue Jobs."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from julius.aws.window import AnalysisWindow
from julius.inventory.model import GlueJob

_MAX_OBJECTS_PER_JOB = 20
_MAX_OBJECT_BYTES = 10 * 1024 * 1024
_MAX_LIST_PAGES = 5


def enrich_glue_shuffle(
    s3_client,
    jobs: list[GlueJob],
    *,
    window: AnalysisWindow,
) -> None:
    cutoff = window.start
    for job in jobs:
        location = _s3_location(job.spark_event_logs_path)
        if location is None:
            continue
        bucket, prefix = location
        objects, listing_complete = _objects(s3_client, bucket, prefix, cutoff)
        spill = 0.0
        shuffle_read = 0.0
        shuffle_write = 0.0
        complete_objects = 0
        for item in objects:
            size = int(item.get("Size", 0) or 0)
            if size <= 0 or size > _MAX_OBJECT_BYTES:
                continue
            try:
                response = s3_client.get_object(Bucket=bucket, Key=item["Key"])
                payload = response["Body"].read()
                if str(item["Key"]).endswith(".gz"):
                    payload = gzip.decompress(payload)
            except Exception:
                continue
            parsed = _parse(payload)
            if parsed["events"] <= 0:
                continue
            spill += parsed["spill"]
            shuffle_read += parsed["shuffle_read"]
            shuffle_write += parsed["shuffle_write"]
            complete_objects += 1
        if complete_objects:
            job.shuffle_spill_bytes = round(spill, 3)
            job.shuffle_read_bytes = round(shuffle_read, 3)
            job.shuffle_write_bytes = round(shuffle_write, 3)
            job.has_spill_evidence = True
            job.spark_event_log_objects_scanned = complete_objects
            job.spark_event_log_evidence_complete = (
                listing_complete and complete_objects == len(objects)
            )


def _s3_location(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    prefix = parsed.path.lstrip("/")
    if not prefix:
        return None
    return parsed.netloc, prefix.rstrip("/") + "/"


def _objects(
    s3_client, bucket: str, prefix: str, cutoff: datetime
) -> tuple[list[dict], bool]:
    candidates = []
    token = None
    listing_complete = True
    for _ in range(_MAX_LIST_PAGES):
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            response = s3_client.list_objects_v2(**kwargs)
        except Exception:
            return [], False
        for item in response.get("Contents", []):
            modified = item.get("LastModified")
            if isinstance(modified, datetime):
                normalized = modified.replace(tzinfo=modified.tzinfo or timezone.utc)
                if normalized < cutoff:
                    continue
            candidates.append(item)
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated") or not token:
            break
    else:
        listing_complete = False
    candidates.sort(
        key=lambda item: item.get("LastModified")
        if isinstance(item.get("LastModified"), datetime)
        else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if len(candidates) > _MAX_OBJECTS_PER_JOB:
        listing_complete = False
    return candidates[:_MAX_OBJECTS_PER_JOB], listing_complete


def _parse(payload: bytes) -> dict[str, float]:
    totals = {
        "spill": 0.0,
        "shuffle_read": 0.0,
        "shuffle_write": 0.0,
        "events": 0.0,
    }
    for raw_line in payload.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        metrics = event.get("Task Metrics") or event.get("TaskMetrics") or {}
        if not metrics:
            continue
        totals["events"] += 1
        totals["spill"] += float(
            metrics.get("Disk Bytes Spilled", metrics.get("DiskBytesSpilled", 0)) or 0
        )
        read = metrics.get("Shuffle Read Metrics") or metrics.get("ShuffleReadMetrics") or {}
        totals["shuffle_read"] += float(
            read.get("Remote Bytes Read", read.get("RemoteBytesRead", 0)) or 0
        ) + float(read.get("Local Bytes Read", read.get("LocalBytesRead", 0)) or 0)
        write = metrics.get("Shuffle Write Metrics") or metrics.get("ShuffleWriteMetrics") or {}
        totals["shuffle_write"] += float(
            write.get("Shuffle Bytes Written", write.get("ShuffleBytesWritten", 0)) or 0
        )
    return totals
