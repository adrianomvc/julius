"""Coleta regras EventBridge que disparam Step Functions."""

from __future__ import annotations

from julius.inventory.model import Schedule


def collect_schedules(events_client) -> list[Schedule]:
    schedules: list[Schedule] = []
    paginator = events_client.get_paginator("list_rules")
    for page in paginator.paginate():
        for rule in page.get("Rules", []):
            if not rule.get("ScheduleExpression"):
                continue
            targets = events_client.list_targets_by_rule(Rule=rule["Name"]).get(
                "Targets", []
            )
            for target in targets:
                arn = target.get("Arn", "")
                if ":states:" not in arn or ":stateMachine:" not in arn:
                    continue
                schedules.append(
                    Schedule(
                        name=rule["Name"],
                        target_type="state_machine",
                        target_name=arn.rsplit(":", 1)[-1],
                    )
                )
    return schedules
