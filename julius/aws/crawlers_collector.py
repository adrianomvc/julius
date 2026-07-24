"""Coleta read-only de Crawlers, histórico mensal e DPU-h reportada pela AWS."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from julius.inventory.model import GlueCrawler
from julius.aws.schedule_frequency import expected_runs_per_month


def collect_crawlers(glue_client, *, now: datetime | None = None) -> list[GlueCrawler]:
    now = now or datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raw_crawlers: list[dict] = []
    paginator = glue_client.get_paginator("get_crawlers")
    for page in paginator.paginate():
        raw_crawlers.extend(page.get("Crawlers", []))

    metrics = _metrics_by_name(glue_client)
    out: list[GlueCrawler] = []
    for raw in raw_crawlers:
        name = str(raw.get("Name") or "")
        histories = _histories(glue_client, name, month_start)
        history_changes = _catalog_changes(histories)
        schedule = raw.get("Schedule", {}) or {}
        last = raw.get("LastCrawl", {}) or {}
        metric = metrics.get(name, {})
        out.append(
            GlueCrawler(
                name=name,
                state=str(raw.get("State") or "READY"),
                last_crawl_status=str(last.get("Status") or ""),
                last_crawl_started_at=_iso(last.get("StartTime")),
                last_error=str(last.get("ErrorMessage") or ""),
                schedule_expression=str(schedule.get("ScheduleExpression") or ""),
                schedule_state=str(schedule.get("State") or "NOT_SCHEDULED"),
                database_name=str(raw.get("DatabaseName") or ""),
                median_runtime_sec=float(metric.get("MedianRuntimeSeconds", 0) or 0),
                last_runtime_sec=float(metric.get("LastRuntimeSeconds", 0) or 0),
                tables_created=history_changes.get(
                    "created", int(metric.get("TablesCreated", 0) or 0)
                ),
                tables_updated=history_changes.get(
                    "updated", int(metric.get("TablesUpdated", 0) or 0)
                ),
                tables_deleted=history_changes.get(
                    "deleted", int(metric.get("TablesDeleted", 0) or 0)
                ),
                runs_mtd=len(histories),
                failures_mtd=sum(
                    1 for item in histories if item.get("State") == "FAILED"
                ),
                dpu_hours_mtd=round(
                    sum(float(item.get("DPUHour", 0) or 0) for item in histories), 4
                ),
                owner_tag=(raw.get("Tags", {}) or {}).get("Owner"),
                crawl_ids_mtd=sorted(
                    str(item["CrawlId"]) for item in histories if item.get("CrawlId")
                ),
                expected_runs_monthly=expected_runs_per_month(
                    str(schedule.get("ScheduleExpression") or "")
                ),
                cost_data_through=now.date().isoformat(),
                recrawl_behavior=str(
                    (raw.get("RecrawlPolicy") or {}).get("RecrawlBehavior")
                    or "CRAWL_EVERYTHING"
                ),
            )
        )
    return out


def _metrics_by_name(glue_client) -> dict[str, dict]:
    try:
        paginator = glue_client.get_paginator("get_crawler_metrics")
        pages = paginator.paginate()
    except Exception:
        try:
            pages = [glue_client.get_crawler_metrics()]
        except Exception:
            return {}
    return {
        str(item.get("CrawlerName")): item
        for page in pages
        for item in page.get("CrawlerMetricsList", [])
        if item.get("CrawlerName")
    }


def _histories(glue_client, name: str, month_start: datetime) -> list[dict]:
    try:
        paginator = glue_client.get_paginator("list_crawls")
        pages = paginator.paginate(CrawlerName=name)
    except Exception:
        try:
            pages = [glue_client.list_crawls(CrawlerName=name)]
        except Exception:
            return []
    histories: list[dict] = []
    for page in pages:
        for item in page.get("Crawls", []):
            started = item.get("StartTime")
            if isinstance(started, datetime):
                normalized = started.replace(tzinfo=started.tzinfo or timezone.utc)
                if normalized < month_start:
                    continue
            histories.append(item)
    return histories


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _catalog_changes(histories: list[dict]) -> dict[str, int]:
    totals = {"created": 0, "updated": 0, "deleted": 0}
    found = False
    aliases = {
        "created": ("TablesCreated", "tablesCreated", "tables_added"),
        "updated": ("TablesUpdated", "tablesUpdated", "tables_updated"),
        "deleted": ("TablesDeleted", "tablesDeleted", "tables_deleted"),
    }
    for item in histories:
        raw = item.get("Summary")
        if not raw:
            continue
        try:
            summary = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(summary, dict):
            continue
        for target, keys in aliases.items():
            for key in keys:
                if key in summary:
                    totals[target] += int(summary[key] or 0)
                    found = True
                    break
    return totals if found else {}
