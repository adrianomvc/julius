"""Diagnóstico IAM read-only estruturado e sanitizado.

Coletores continuam livres para registrar gaps textuais legíveis. Esta camada
converte somente operações conhecidas ou ações explicitamente declaradas pela
fonte; nunca inventa uma permissão a partir de uma mensagem da AWS.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from julius.collection.models.health import IamGap

_ACTION = re.compile(r"\b([a-z][a-z0-9-]*:[A-Z][A-Za-z0-9]*)\b")
_RESOURCE = re.compile(r"^[a-z0-9_]+(?:\[([^]]+)\]|:([^:]+))", re.I)

IAM_ACTIONS: dict[str, str] = {
    "batch_get_query_execution": "athena:BatchGetQueryExecution",
    "describe_state_machine": "states:DescribeStateMachine",
    "get_capacity_assignment": "athena:GetCapacityAssignmentConfiguration",
    "get_capacity_reservation": "athena:GetCapacityReservation",
    "get_crawler_metrics": "glue:GetCrawlerMetrics",
    "get_crawlers": "glue:GetCrawlers",
    "get_execution_history": "states:GetExecutionHistory",
    "get_job_runs": "glue:GetJobRuns",
    "get_jobs": "glue:GetJobs",
    "get_query_execution": "athena:GetQueryExecution",
    "get_work_group": "athena:GetWorkGroup",
    "get_bucket_lifecycle_configuration": "s3:GetLifecycleConfiguration",
    "get_bucket_logging": "s3:GetBucketLogging",
    "get_bucket_metadata_configuration": "s3:GetBucketMetadataTableConfiguration",
    "get_tables": "glue:GetTables",
    "list_capacity_reservations": "athena:ListCapacityReservations",
    "list_bucket_analytics_configurations": "s3:GetAnalyticsConfiguration",
    "list_bucket_intelligent_tiering_configurations": (
        "s3:GetIntelligentTieringConfiguration"
    ),
    "list_crawls": "glue:ListCrawls",
    "list_executions": "states:ListExecutions",
    "list_job_runs": "databrew:ListJobRuns",
    "list_jobs": "databrew:ListJobs",
    "list_query_executions": "athena:ListQueryExecutions",
    "list_rules": "events:ListRules",
    "list_schedules": "databrew:ListSchedules",
    "list_sessions": "glue:ListSessions",
    "list_state_machines": "states:ListStateMachines",
    "list_storage_lens_configurations": "s3:ListStorageLensConfigurations",
    "list_targets_by_rule": "events:ListTargetsByRule",
    "list_work_groups": "athena:ListWorkGroups",
    "lookup_events": "cloudtrail:LookupEvents",
}


class DeclaredPermissionDenied(PermissionError):
    """Ação explicitamente negada pelo operador; nenhuma chamada AWS ocorreu."""

    def __init__(self, action: str) -> None:
        self.action = action
        self.response = {"Error": {"Code": "AccessDeniedException"}}
        super().__init__(f"permissão declarada como indisponível: {action}")


def action_for_operation(operation: str, declared: str = "") -> str:
    operation = _operation(operation)
    if operation in IAM_ACTIONS:
        return IAM_ACTIONS[operation]
    for action in _ACTION.findall(declared):
        if _comparable(action.split(":", 1)[1]) == _comparable(operation):
            return action
    return ""


def action_for_call(service: str, operation: str) -> str:
    known = action_for_operation(operation)
    if known:
        return known
    namespace = {"stepfunctions": "states"}.get(service, service)
    return f"{namespace}:{_pascal(operation)}"


def gaps_from_text(
    gaps: Iterable[str],
    *,
    declared_actions: str = "",
) -> list[IamGap]:
    from julius.collection.models.health import IamGap

    aggregated: dict[tuple[str, str], IamGap] = {}
    for text in gaps:
        if text.rsplit(": ", 1)[-1] != "permission_denied":
            continue
        operation = _operation(text)
        action = action_for_operation(operation, declared_actions)
        if not action:
            continue
        service = action.split(":", 1)[0]
        gap = aggregated.setdefault(
            (service, operation),
            IamGap(
                service=service,
                operation=operation,
                iam_action=action,
                affected_resources=0,
            ),
        )
        gap.affected_resources += 1
        resource = _resource(text)
        if resource and resource not in gap.examples and len(gap.examples) < 3:
            gap.examples.append(resource)
    return sorted(aggregated.values(), key=lambda item: (item.service, item.operation))


def gap_from_exception(exc: Exception) -> IamGap | None:
    from julius.collection.models.health import IamGap

    service = str(getattr(exc, "julius_service", "") or "")
    operation = str(getattr(exc, "julius_operation", "") or "")
    if not service or not operation:
        return None
    action = action_for_operation(operation)
    if not action:
        action = action_for_call(service, operation)
    return IamGap(
        service=service,
        operation=operation,
        iam_action=action,
        affected_resources=1,
    )


def _operation(text: str) -> str:
    token = text.split(": ", 1)[0]
    token = token.split("[", 1)[0].split(":", 1)[0].split("(", 1)[0]
    return token.strip().lower()


def _resource(text: str) -> str:
    match = _RESOURCE.match(text.split(": ", 1)[0])
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "")


def _pascal(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_") if part)


def _comparable(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower()).removesuffix("s")
