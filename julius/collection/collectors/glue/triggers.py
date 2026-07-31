"""Coleta read-only de triggers/workflows Glue e suas ações."""

from __future__ import annotations

from julius.collection.models import GlueTrigger
from julius.collection.ownership_tags import owner_from_tags
from julius.collection.schedule_frequency import expected_runs_per_month


def collect_triggers(glue_client) -> list[GlueTrigger]:
    out: list[GlueTrigger] = []
    paginator = glue_client.get_paginator("get_triggers")
    for page in paginator.paginate():
        for raw in page.get("Triggers", []):
            actions = raw.get("Actions", []) or []
            out.append(
                GlueTrigger(
                    name=str(raw.get("Name") or ""),
                    trigger_type=str(raw.get("Type") or "ON_DEMAND"),
                    state=str(raw.get("State") or ""),
                    schedule_expression=str(raw.get("Schedule") or ""),
                    workflow_name=str(raw.get("WorkflowName") or ""),
                    job_names=sorted(
                        {str(action["JobName"]) for action in actions if action.get("JobName")}
                    ),
                    crawler_names=sorted(
                        {
                            str(action["CrawlerName"])
                            for action in actions
                            if action.get("CrawlerName")
                        }
                    ),
                    owner_tag=owner_from_tags(raw.get("Tags")),
                    expected_runs_monthly=expected_runs_per_month(
                        str(raw.get("Schedule") or "")
                    ),
                )
            )
    return out
