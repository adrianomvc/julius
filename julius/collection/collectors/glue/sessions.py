"""Coletor de Glue Interactive Sessions (estrutura básica).

Campos de ociosidade (`idle_hours_per_day`) exigem CloudWatch/atividade — ficam
0 aqui; o detector de sessão ociosa só dispara quando essa métrica for adicionada.
"""

from __future__ import annotations

from datetime import datetime, timezone

from julius.collection.models import InteractiveSession
from julius.collection.window import AnalysisWindow
from julius.config import DPU_PER_WORKER


def collect_sessions(
    glue_client, *, window: AnalysisWindow
) -> list[InteractiveSession]:
    now = window.end
    out: list[InteractiveSession] = []
    paginator = glue_client.get_paginator("list_sessions")
    for page in paginator.paginate():
        for s in page.get("Sessions", []):
            max_capacity = (
                float(s["MaxCapacity"]) if s.get("MaxCapacity") is not None else None
            )
            workers = (
                int(s["NumberOfWorkers"])
                if s.get("NumberOfWorkers") is not None
                else None
            )
            worker_type = s.get("WorkerType")
            dpu = float(
                max_capacity
                or ((workers or 0) * DPU_PER_WORKER.get(worker_type or "", 0))
                or 5
            )
            activity = _statement_activity(
                glue_client,
                str(s.get("Id") or ""),
                s.get("CreatedOn"),
                s.get("CompletedOn"),
                now,
            )
            created_on = _dt(s.get("CreatedOn"))
            completed_on = _dt(s.get("CompletedOn"))
            reported_seconds = float(s.get("DPUSeconds", 0) or 0)
            # `DPUSeconds` cobre a sessão inteira; só pode ser atribuído à
            # janela quando a sessão inteira cabe dentro dela.
            fully_observed_window = (
                created_on is not None
                and created_on >= window.start
                and (completed_on is None or completed_on <= window.end)
            )
            overlap_seconds = window.overlap_seconds(created_on, completed_on)
            reported_window = reported_seconds if fully_observed_window else 0.0
            estimated_window = (
                0.0
                if fully_observed_window and reported_seconds > 0
                else overlap_seconds * dpu / 3600.0
            )
            out.append(
                InteractiveSession(
                    session_id=s.get("Id", "?"),
                    dpu=dpu,
                    worker_type=worker_type,
                    number_of_workers=workers,
                    max_capacity=max_capacity,
                    glue_version=str(s.get("GlueVersion") or ""),
                    idle_timeout_min=int(s.get("IdleTimeout", 2880) or 2880),
                    status=s.get("Status", "READY"),
                    idle_hours_per_day=activity["idle_hours_per_day"],
                    owner_tag=(s.get("Tags", {}) or {}).get("Owner"),
                    created_on=_iso(s.get("CreatedOn")),
                    completed_on=_iso(s.get("CompletedOn")),
                    execution_time_sec=float(s.get("ExecutionTime", 0) or 0),
                    dpu_seconds=reported_seconds,
                    dpu_seconds_window=reported_window,
                    estimated_dpu_hours_window=round(estimated_window, 4),
                    window_end=window.data_through.isoformat(),
                    window_days=window.days,
                    observed_runs=activity["statement_count"],
                    coverage_days=activity["coverage_days"],
                    last_activity_at=activity["last_activity_at"],
                    activity_evidence=activity["available"],
                    statement_ids=activity["statement_ids"],
                )
            )
    return out


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _statement_activity(
    glue_client,
    session_id: str,
    created_on,
    completed_on,
    now: datetime,
) -> dict:
    statements: list[dict] = []
    token = None
    try:
        while True:
            kwargs = {"SessionId": session_id}
            if token:
                kwargs["NextToken"] = token
            response = glue_client.list_statements(**kwargs)
            statements.extend(response.get("Statements", []))
            token = response.get("NextToken")
            if not token:
                break
    except Exception:
        return {
            "available": False,
            "statement_count": 0,
            "coverage_days": 0,
            "idle_hours_per_day": 0.0,
            "last_activity_at": "",
            "statement_ids": [],
        }

    start = _dt(created_on) or now
    end = _dt(completed_on) or now
    elapsed_sec = max(0.0, (end - start).total_seconds())
    busy_sec = 0.0
    activity_times: list[datetime] = []
    for statement in statements:
        statement_start = _dt(statement.get("StartedOn"))
        statement_end = _dt(statement.get("CompletedOn"))
        if statement_start:
            activity_times.append(statement_start)
        if statement_end:
            activity_times.append(statement_end)
        if statement_start and statement_end:
            busy_sec += max(0.0, (statement_end - statement_start).total_seconds())
    active_days = max(1.0, elapsed_sec / 86400.0)
    idle_hours_per_day = max(0.0, elapsed_sec - busy_sec) / 3600.0 / active_days
    return {
        "available": True,
        "statement_count": len(statements),
        "coverage_days": max(1, round(active_days)),
        "idle_hours_per_day": round(idle_hours_per_day, 3),
        "last_activity_at": _iso(max(activity_times)) if activity_times else "",
        "statement_ids": [
            str(statement["Id"]) for statement in statements if statement.get("Id") is not None
        ],
    }


def _dt(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc)
