"""Coleta Step Functions e deriva chamadas a Glue Jobs da definição ASL."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from julius.aws.window import AnalysisWindow

from julius.inventory.model import StateMachine


def collect_state_machines(
    client,
    *,
    window: AnalysisWindow,
) -> list[StateMachine]:
    cutoff = window.start
    months = max(1.0, window.days / 30.0)
    machines: list[StateMachine] = []

    paginator = client.get_paginator("list_state_machines")
    for page in paginator.paginate():
        for summary in page.get("stateMachines", []):
            arn = summary["stateMachineArn"]
            detail = client.describe_state_machine(stateMachineArn=arn)
            definition = _json(detail.get("definition", "{}"))
            executions = _executions(client, arn, cutoff)
            durations = [
                (item["stopDate"] - item["startDate"]).total_seconds()
                for item in executions
                if item.get("stopDate") and item.get("startDate")
            ]
            machines.append(
                StateMachine(
                    name=summary["name"],
                    type=detail.get("type", "STANDARD"),
                    executions_per_month=round(len(executions) / months),
                    avg_duration_sec=round(sum(durations) / len(durations), 1)
                    if durations
                    else 0.0,
                    observed_runs=len(executions),
                    coverage_days=window.days,
                    glue_jobs=sorted(_glue_jobs(definition)),
                    has_polling_loop=_has_polling_loop(definition),
                )
            )
    return machines


def _executions(client, arn: str, cutoff: datetime) -> list[dict]:
    executions: list[dict] = []
    paginator = client.get_paginator("list_executions")
    for page in paginator.paginate(stateMachineArn=arn):
        for item in page.get("executions", []):
            started = item.get("startDate")
            if started and started.replace(tzinfo=started.tzinfo or timezone.utc) < cutoff:
                return executions
            executions.append(item)
    return executions


def _json(value: str) -> dict:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _states(definition: dict):
    for state in (definition.get("States") or {}).values():
        yield state
        for branch in state.get("Branches", []) or []:
            yield from _states(branch)
        if state.get("ItemProcessor"):
            yield from _states(state["ItemProcessor"])
        if state.get("Iterator"):
            yield from _states(state["Iterator"])


def _glue_jobs(definition: dict) -> set[str]:
    jobs: set[str] = set()
    for state in _states(definition):
        resource = str(state.get("Resource", "")).lower()
        if "glue:startjobrun" not in resource:
            continue
        params = state.get("Parameters", {}) or {}
        name = params.get("JobName") or params.get("JobName.$")
        if name and not str(name).startswith("$"):
            jobs.add(str(name))
    return jobs


def _has_polling_loop(definition: dict) -> bool:
    states = definition.get("States", {}) or {}
    for name, state in states.items():
        if state.get("Type") != "Wait":
            continue
        seen: set[str] = set()
        current = state.get("Next")
        while current and current not in seen and current in states:
            seen.add(current)
            candidate = states[current]
            if candidate.get("Next") == name:
                return True
            current = candidate.get("Next")
    return False
