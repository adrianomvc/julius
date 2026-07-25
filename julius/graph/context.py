"""Aplica contexto do grafo às oportunidades sem inventar evidências."""

from __future__ import annotations

from julius.graph.actor import resolve_actor
from julius.graph.assets import AssetKey
from julius.graph.edges import EdgeType
from julius.graph.ownership import resolve_owner
from julius.graph.process_builder import ProcessGraph
from julius.inventory.model import Account
from julius.opportunities import prioritizer
from julius.opportunities.base import Opportunity

_FLOW_EDGES = {
    EdgeType.SCHEDULE_TRIGGERS_STATE_MACHINE,
    EdgeType.STATE_MACHINE_RUNS_JOB,
    EdgeType.JOB_WRITES_TABLE,
    EdgeType.TABLE_USED_BY_ACCOUNT,
    EdgeType.TABLE_PUBLISHED_BY_DATAWARM,
}


def enrich_opportunities(
    account: Account, graph: ProcessGraph, opportunities: list[Opportunity]
) -> None:
    for opportunity in opportunities:
        old_risk = _inferred_risk(opportunity)
        if not opportunity.owner:
            owner = resolve_owner(
                account, opportunity.asset_type, opportunity.asset_name
            )
            opportunity.owner = owner.owner
            opportunity.owner_source = owner.source
            opportunity.owner_confidence = owner.confidence
            opportunity.owner_event_time = owner.event_time
            opportunity.owner_event_name = owner.event_name

        if not opportunity.actor:
            actor = resolve_actor(
                account, opportunity.asset_type, opportunity.asset_name
            )
            opportunity.actor = actor.actor
            opportunity.actor_source = actor.source
            opportunity.actor_confidence = actor.confidence

        process_rows = [
            row
            for row in account.process_costs
            if opportunity.asset_name in row.component_names
            or opportunity.asset_name == row.process_name
            or (
                opportunity.source_process is not None
                and opportunity.source_process in row.component_names
            )
        ]
        if process_rows:
            opportunity.process_cost_window = round(
                sum(row.total_cost_window for row in process_rows), 2
            )
            opportunity.process_forecast_eom = round(
                sum(row.forecast_cost_eom for row in process_rows), 2
            )
            opportunity.window_end = max(
                row.data_through for row in process_rows
            )
        opportunity.evidence_refs = _evidence_refs(account, opportunity)

        key = AssetKey(
            account.account_id, opportunity.asset_type, opportunity.asset_name
        )
        consumers, datawarm = _reach(graph, key)
        opportunity.downstream_consumers = consumers
        opportunity.process_criticality = min(
            1.0, consumers / 5.0 + (0.2 if datawarm else 0.0)
        )
        if consumers:
            evidence = f"Grafo: cadeia atende {consumers} conta(s) Consumer"
            if evidence not in opportunity.evidence:
                opportunity.evidence.append(evidence)
        if opportunity.process_criticality >= 0.6:
            risk = (
                "Mudança afeta cadeia compartilhada; validar dependências e janela "
                "antes de aplicar."
            )
            if risk not in opportunity.risks:
                opportunity.risks.append(risk)

        graph_risk = min(1.2, old_risk + 0.3 * opportunity.process_criticality)
        prioritizer.assign(opportunity, risk=graph_risk)


def _inferred_risk(opportunity: Opportunity) -> float:
    denominator = opportunity.gain_score * opportunity.confidence
    if denominator <= 0:
        return 0.6
    return max(0.0, min(1.2, opportunity.strategic_priority / denominator))


def _reach(graph: ProcessGraph, start: AssetKey) -> tuple[int, bool]:
    if start not in graph.nodes:
        return 0, False
    queue = [start]
    seen = {start}
    consumers: set[str] = set()
    aggregate_consumers = 0
    datawarm = False
    while queue:
        current = queue.pop(0)
        node = graph.nodes.get(current)
        if node and current.kind == "table":
            aggregate_consumers = max(
                aggregate_consumers,
                int(node.attributes.get("consuming_accounts", 0) or 0),
            )
        for edge in graph.edges_from(current):
            if edge.type not in _FLOW_EDGES:
                continue
            target = edge.target
            if target.kind == "consumer_account":
                consumers.add(target.name)
            if target.kind == "datawarm":
                datawarm = True
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return max(len(consumers), aggregate_consumers), datawarm


def _evidence_refs(account: Account, opportunity: Opportunity) -> list[dict]:
    common = {
        "resource_type": opportunity.asset_type,
        "resource_name": opportunity.asset_name,
        "collected_at": account.generated_at,
    }
    if opportunity.asset_type == "glue_job":
        job = account.job_by_name(opportunity.asset_name)
        if job is not None:
            return [
                {
                    **common,
                    "source": "Glue GetJobRuns",
                    "run_ids": job.run_ids_in_window,
                    "data_through": job.window_end,
                    "actual_dpu_hours": job.actual_dpu_hours_window,
                    "estimated_dpu_hours": job.estimated_dpu_hours_window,
                }
            ]
    collections = {
        "glue_session": (account.interactive_sessions, "session_id", "statement_ids"),
        "glue_crawler": (account.glue_crawlers, "name", "crawl_ids_in_window"),
        "databrew_job": (account.databrew_jobs, "name", "run_ids_in_window"),
    }
    spec = collections.get(opportunity.asset_type)
    if spec is None:
        return []
    items, name_field, ids_field = spec
    asset = next(
        (
            item
            for item in items
            if getattr(item, name_field, None) == opportunity.asset_name
        ),
        None,
    )
    if asset is None:
        return []
    return [
        {
            **common,
            "source": {
                "glue_session": "Glue ListStatements",
                "glue_crawler": "Glue ListCrawls",
                "databrew_job": "DataBrew ListJobRuns",
            }[opportunity.asset_type],
            "execution_ids": list(getattr(asset, ids_field, [])),
        }
    ]
