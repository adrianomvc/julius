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

        if not opportunity.actor:
            actor = resolve_actor(
                account, opportunity.asset_type, opportunity.asset_name
            )
            opportunity.actor = actor.actor
            opportunity.actor_source = actor.source
            opportunity.actor_confidence = actor.confidence

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
