"""Coleta regras EventBridge que disparam Step Functions."""

from __future__ import annotations

from julius.collection.collectors.paginate import safe_call, safe_pages
from julius.collection.models import Schedule
from julius.collection.schedule_frequency import expected_runs_per_month


def collect_schedules(
    events_client, *, gaps: list[str] | None = None
) -> list[Schedule]:
    listagem = safe_pages(events_client, "list_rules", "Rules")
    if gaps is not None and not listagem.complete:
        gaps.append(f"list_rules: {listagem.error_category or 'incompleto'}")
    schedules: list[Schedule] = []
    for rule in listagem.items:
        if not rule.get("ScheduleExpression"):
            continue
        # Uma regra negada perde os alvos dela, e só dela. Antes derrubava a
        # listagem inteira, e o grafo de processos ficava sem raiz nenhuma.
        resposta, falha = safe_call(
            events_client, "list_targets_by_rule", Rule=rule["Name"]
        )
        if falha and gaps is not None:
            gaps.append(f"list_targets_by_rule: {falha}")
        for target in resposta.get("Targets", []) or []:
            arn = target.get("Arn", "")
            if ":states:" not in arn or ":stateMachine:" not in arn:
                continue
            schedules.append(
                Schedule(
                    name=rule["Name"],
                    target_type="state_machine",
                    target_name=arn.rsplit(":", 1)[-1],
                    expression=str(rule.get("ScheduleExpression") or ""),
                    state=str(rule.get("State") or "ENABLED"),
                    expected_runs_monthly=expected_runs_per_month(
                        str(rule.get("ScheduleExpression") or "")
                    ),
                )
            )
    return schedules
