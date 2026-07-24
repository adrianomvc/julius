"""Constrói o grafo fim a fim a partir do inventário e suas evidências."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.graph.assets import Asset, AssetKey
from julius.graph.edges import Edge, EdgeType
from julius.graph.lineage import build_lineage
from julius.graph.ownership import resolve_owner
from julius.inventory.model import Account


@dataclass
class ProcessGraph:
    nodes: dict[AssetKey, Asset] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def add_asset(self, key: AssetKey, **attributes: object) -> AssetKey:
        if key not in self.nodes:
            self.nodes[key] = Asset(key, dict(attributes))
        else:
            self.nodes[key].attributes.update(attributes)
        return key

    def add_edge(self, edge: Edge) -> None:
        self.add_asset(edge.source)
        self.add_asset(edge.target)
        for position, existing in enumerate(self.edges):
            same_relation = (
                existing.source == edge.source
                and existing.target == edge.target
                and existing.type == edge.type
            )
            if same_relation:
                if edge.confidence > existing.confidence:
                    self.edges[position] = edge
                return
        self.edges.append(edge)

    def edges_from(
        self, key: AssetKey, edge_type: EdgeType | None = None
    ) -> list[Edge]:
        return [
            edge
            for edge in self.edges
            if edge.source == key and (edge_type is None or edge.type == edge_type)
        ]

    def edges_to(
        self, key: AssetKey, edge_type: EdgeType | None = None
    ) -> list[Edge]:
        return [
            edge
            for edge in self.edges
            if edge.target == key and (edge_type is None or edge.type == edge_type)
        ]

    def has_edge(self, edge_type: EdgeType, source: AssetKey, target: AssetKey) -> bool:
        return any(
            edge.type == edge_type and edge.source == source and edge.target == target
            for edge in self.edges
        )


def build_process_graph(account: Account) -> ProcessGraph:
    graph = ProcessGraph()
    account_id = account.account_id

    collections = (
        ("schedule", account.schedules, "name"),
        ("state_machine", account.state_machines, "name"),
        ("glue_job", account.glue_jobs, "name"),
        ("glue_session", account.interactive_sessions, "session_id"),
        ("glue_crawler", account.glue_crawlers, "name"),
        ("glue_trigger", account.glue_triggers, "name"),
        ("databrew_job", account.databrew_jobs, "name"),
        ("athena_query", account.athena_queries, "query_id"),
        ("sagemaker_app", account.sagemaker_apps, "name"),
        ("sagemaker_endpoint", account.sagemaker_endpoints, "name"),
        ("table", account.tables, "name"),
    )
    for kind, items, name_field in collections:
        for item in items:
            attributes = {"owner_tag": getattr(item, "owner_tag", None)}
            if kind == "table":
                attributes.update(
                    consuming_accounts=item.consuming_accounts,
                    consuming_communities=item.consuming_communities,
                    datawarm_published=item.datawarm_published,
                )
            graph.add_asset(
                AssetKey(account_id, kind, getattr(item, name_field)),
                **attributes,
            )

    for schedule in account.schedules:
        if schedule.target_type != "state_machine" or not schedule.target_name:
            continue
        graph.add_edge(
            Edge(
                AssetKey(account_id, "schedule", schedule.name),
                AssetKey(account_id, "state_machine", schedule.target_name),
                EdgeType.SCHEDULE_TRIGGERS_STATE_MACHINE,
                "target do agendamento",
            )
        )

    for machine in account.state_machines:
        for schedule_name in machine.schedule_names:
            graph.add_edge(
                Edge(
                    AssetKey(account_id, "schedule", schedule_name),
                    AssetKey(account_id, "state_machine", machine.name),
                    EdgeType.SCHEDULE_TRIGGERS_STATE_MACHINE,
                    "agendamento declarado na state machine",
                    0.9,
                )
            )
        for job_name in machine.glue_jobs:
            graph.add_edge(
                Edge(
                    AssetKey(account_id, "state_machine", machine.name),
                    AssetKey(account_id, "glue_job", job_name),
                    EdgeType.STATE_MACHINE_RUNS_JOB,
                    "definição ASL da state machine",
                    0.95,
                )
            )

    for trigger in account.glue_triggers:
        trigger_key = AssetKey(account_id, "glue_trigger", trigger.name)
        for job_name in trigger.job_names:
            graph.add_edge(
                Edge(
                    trigger_key,
                    AssetKey(account_id, "glue_job", job_name),
                    EdgeType.GLUE_TRIGGER_RUNS_JOB,
                    "ação declarada no Glue Trigger",
                    1.0,
                )
            )
        for crawler_name in trigger.crawler_names:
            graph.add_edge(
                Edge(
                    trigger_key,
                    AssetKey(account_id, "glue_crawler", crawler_name),
                    EdgeType.GLUE_TRIGGER_RUNS_CRAWLER,
                    "ação declarada no Glue Trigger",
                    1.0,
                )
            )
    for edge in build_lineage(account):
        graph.add_edge(edge)

    for table in account.tables:
        table_key = AssetKey(account_id, "table", table.name)
        for consumer in table.used_by_accounts:
            graph.add_edge(
                Edge(
                    table_key,
                    AssetKey(consumer, "consumer_account", consumer),
                    EdgeType.TABLE_USED_BY_ACCOUNT,
                    "tabela oficial de toques",
                    0.95,
                )
            )
        if table.datawarm_published:
            graph.add_edge(
                Edge(
                    table_key,
                    AssetKey(account_id, "datawarm", "DataWarm"),
                    EdgeType.TABLE_PUBLISHED_BY_DATAWARM,
                    "configuração/publicação DataWarm",
                    0.95,
                )
            )

    for key in list(graph.nodes):
        if key.kind in {"squad", "consumer_account", "datawarm"}:
            continue
        attribution = resolve_owner(account, key.kind, key.name)
        if attribution.owner:
            graph.add_edge(
                Edge(
                    key,
                    AssetKey(account_id, "squad", attribution.owner),
                    EdgeType.ASSET_OWNED_BY_SQUAD,
                    attribution.source,
                    attribution.confidence,
                )
            )
    return graph
