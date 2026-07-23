"""Normaliza autoria de recursos a partir do Event history do CloudTrail."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from julius.inventory.model import ActorEvent

_EVENT_TYPES = {
    "CreateJob": ("glue_job", ("jobName", "name")),
    "UpdateJob": ("glue_job", ("jobName", "name")),
    "StartJobRun": ("glue_job", ("jobName",)),
    "CreateSession": ("glue_session", ("id", "sessionId")),
    "StartQueryExecution": ("athena_query", ("queryExecutionId",)),
    "CreateStateMachine": ("state_machine", ("name", "stateMachineArn")),
    "StartExecution": ("state_machine", ("stateMachineArn",)),
    "CreateNotebookInstance": ("sagemaker_app", ("notebookInstanceName",)),
    "CreateTrainingJob": ("sagemaker_app", ("trainingJobName",)),
    "CreateApp": ("sagemaker_app", ("appName",)),
    "CreateEndpoint": ("sagemaker_endpoint", ("endpointName",)),
}


def collect_actor_events(
    cloudtrail_client,
    *,
    lookback_days: int = 90,
    now: datetime | None = None,
) -> list[ActorEvent]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    events: list[ActorEvent] = []
    paginator = cloudtrail_client.get_paginator("lookup_events")
    for page in paginator.paginate(StartTime=start, EndTime=now):
        for outer in page.get("Events", []):
            event_name = outer.get("EventName", "")
            if event_name not in _EVENT_TYPES:
                continue
            try:
                raw = json.loads(outer.get("CloudTrailEvent") or "{}")
            except json.JSONDecodeError:
                raw = {}
            resource_type, keys = _EVENT_TYPES[event_name]
            resource_name = _resource_name(raw, outer, keys)
            if not resource_name:
                continue
            identity = raw.get("userIdentity", {}) or {}
            session_context = identity.get("sessionContext", {}) or {}
            source_identity = identity.get("sourceIdentity") or session_context.get(
                "sourceIdentity"
            )
            event_time = outer.get("EventTime") or raw.get("eventTime") or ""
            if hasattr(event_time, "isoformat"):
                event_time = event_time.isoformat()
            events.append(
                ActorEvent(
                    resource_type=resource_type,
                    resource_name=resource_name,
                    event_name=event_name,
                    event_time=str(event_time),
                    source_identity=source_identity,
                    user_arn=identity.get("arn"),
                )
            )
    return events


def _resource_name(raw: dict, outer: dict, keys: tuple[str, ...]) -> str | None:
    request = raw.get("requestParameters", {}) or {}
    response = raw.get("responseElements", {}) or {}
    for key in keys:
        value = request.get(key) or response.get(key)
        if value:
            return _short_name(str(value))
    for resource in outer.get("Resources", []) or []:
        value = resource.get("ResourceName")
        if value:
            return _short_name(str(value))
    return None


def _short_name(value: str) -> str:
    value = value.rstrip("/")
    if "/" in value:
        return value.rsplit("/", 1)[-1]
    if value.startswith("arn:") and ":" in value:
        return value.rsplit(":", 1)[-1]
    return value
