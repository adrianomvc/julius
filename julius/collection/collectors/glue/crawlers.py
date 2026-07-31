"""Coleta read-only de Crawlers, histórico mensal e DPU-h reportada pela AWS."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from julius.collection.collectors.paginate import safe_pages
from julius.collection.models import GlueCrawler
from julius.collection.ownership_tags import owner_from_tags
from julius.collection.schedule_frequency import expected_runs_per_month
from julius.collection.window import AnalysisWindow


def collect_crawlers(
    glue_client, *, window: AnalysisWindow, gaps: list[str] | None = None
) -> list[GlueCrawler]:
    listagem = safe_pages(glue_client, "get_crawlers", "Crawlers")
    if gaps is not None and not listagem.complete:
        gaps.append(f"get_crawlers: {listagem.error_category or 'incompleto'}")
    raw_crawlers = listagem.items

    metrics = _metrics_by_name(glue_client, gaps)
    out: list[GlueCrawler] = []
    for raw in raw_crawlers:
        name = str(raw.get("Name") or "")
        histories = _histories(glue_client, name, window, gaps)
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
                runs_in_window=len(histories),
                failures_in_window=sum(
                    1 for item in histories if item.get("State") == "FAILED"
                ),
                dpu_hours_window=round(
                    sum(float(item.get("DPUHour", 0) or 0) for item in histories), 4
                ),
                owner_tag=owner_from_tags(raw.get("Tags")),
                crawl_ids_in_window=sorted(
                    str(item["CrawlId"]) for item in histories if item.get("CrawlId")
                ),
                expected_runs_monthly=expected_runs_per_month(
                    str(schedule.get("ScheduleExpression") or "")
                ),
                window_end=window.data_through.isoformat(),
                coverage_days=window.days,
                window_days=window.days,
                recrawl_behavior=str(
                    (raw.get("RecrawlPolicy") or {}).get("RecrawlBehavior")
                    or "CRAWL_EVERYTHING"
                ),
            )
        )
    return out


def _metrics_by_name(glue_client, gaps: list[str] | None = None) -> dict[str, dict]:
    resultado = safe_pages(glue_client, "get_crawler_metrics", "CrawlerMetricsList")
    if gaps is not None and not resultado.complete:
        gaps.append(f"get_crawler_metrics: {resultado.error_category or 'incompleto'}")
    return {
        str(item.get("CrawlerName")): item
        for item in resultado.items
        if item.get("CrawlerName")
    }


def _histories(
    glue_client, name: str, window: AnalysisWindow, gaps: list[str] | None = None
) -> list[dict]:
    """Execuções de **um** crawler. Negado aqui não zera os outros crawlers."""
    resultado = safe_pages(glue_client, "list_crawls", "Crawls", CrawlerName=name)
    if gaps is not None and not resultado.complete:
        gaps.append(f"list_crawls: {resultado.error_category or 'incompleto'}")
    histories: list[dict] = []
    for item in resultado.items:
        started = item.get("StartTime")
        if isinstance(started, datetime):
            normalized = started.replace(tzinfo=started.tzinfo or timezone.utc)
            if not window.contains(normalized):
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
