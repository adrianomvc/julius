"""Grafo de processos, ownership e atribuição de ator do MVP 2."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from julius.aws.cloudtrail_collector import collect_actor_events
from julius.graph import (
    AssetKey,
    EdgeType,
    build_process_graph,
    resolve_actor,
    resolve_owner,
)
from julius.inventory.model import (
    Account,
    ActorEvent,
    GlueJob,
    Schedule,
    StateMachine,
    Table,
)
from julius.pipeline import analyze_account


def _process_account() -> Account:
    return Account(
        account_id="consumer-graph",
        schedules=[
            Schedule(
                name="cron-diario",
                target_type="state_machine",
                target_name="orquestra",
            )
        ],
        state_machines=[
            StateMachine(name="orquestra", glue_jobs=["transforma"])
        ],
        glue_jobs=[
            GlueJob(
                name="transforma",
                worker_type="G.2X",
                number_of_workers=20,
                auto_scaling=False,
                runs_per_month=30,
                avg_execution_sec=3600,
                avg_cpu_load=0.15,
                observed_runs=30,
                coverage_days=90,
                owner_tag="Squad Dados",
                reads_tables=["entrada"],
                writes_tables=["saida"],
            )
        ],
        tables=[
            Table(name="entrada", touches_90d=100),
            Table(
                name="saida",
                written_by="transforma",
                consuming_accounts=5,
                consuming_communities=3,
                used_by_accounts=["consumer-a", "consumer-b"],
                datawarm_published=True,
            ),
        ],
    )


def test_builds_schedule_to_datawarm_chain():
    account = _process_account()
    graph = build_process_graph(account)
    aid = account.account_id

    assert graph.has_edge(
        EdgeType.SCHEDULE_TRIGGERS_STATE_MACHINE,
        AssetKey(aid, "schedule", "cron-diario"),
        AssetKey(aid, "state_machine", "orquestra"),
    )
    assert graph.has_edge(
        EdgeType.STATE_MACHINE_RUNS_JOB,
        AssetKey(aid, "state_machine", "orquestra"),
        AssetKey(aid, "glue_job", "transforma"),
    )
    assert graph.has_edge(
        EdgeType.JOB_READS_TABLE,
        AssetKey(aid, "glue_job", "transforma"),
        AssetKey(aid, "table", "entrada"),
    )
    assert graph.has_edge(
        EdgeType.JOB_WRITES_TABLE,
        AssetKey(aid, "glue_job", "transforma"),
        AssetKey(aid, "table", "saida"),
    )
    assert graph.has_edge(
        EdgeType.TABLE_PUBLISHED_BY_DATAWARM,
        AssetKey(aid, "table", "saida"),
        AssetKey(aid, "datawarm", "DataWarm"),
    )


def test_ownership_precedence():
    account = _process_account()
    table = account.tables[1]

    table.owner_tag = "Tag Owner"
    table.corporate_owner = "Cadastro Owner"
    table.datawarm_owner = "DataWarm Owner"
    table.primary_community = "Comunidade A"
    assert resolve_owner(account, "table", "saida").owner == "Tag Owner"

    table.owner_tag = None
    assert resolve_owner(account, "table", "saida").owner == "Cadastro Owner"
    table.corporate_owner = None
    assert resolve_owner(account, "table", "saida").owner == "DataWarm Owner"
    table.datawarm_owner = None
    assert resolve_owner(account, "table", "saida").owner == "Squad Dados"
    account.glue_jobs[0].owner_tag = None
    assert resolve_owner(account, "table", "saida").owner == "Comunidade A"


def test_actor_precedence_source_identity_then_sso_session():
    account = Account(
        account_id="consumer-actor",
        glue_jobs=[GlueJob(name="job-sem-tag")],
        actor_events=[
            ActorEvent(
                resource_type="glue_job",
                resource_name="job-sem-tag",
                event_name="StartJobRun",
                event_time="2026-07-22T10:00:00Z",
                source_identity="pessoa@empresa.com",
                user_arn="arn:aws:sts::123:assumed-role/Role/shared-session",
            )
        ],
    )
    actor = resolve_actor(account, "glue_job", "job-sem-tag")
    assert actor.actor == "pessoa@empresa.com"
    assert actor.source == "CloudTrail sourceIdentity"

    account.actor_events[0].source_identity = None
    actor = resolve_actor(account, "glue_job", "job-sem-tag")
    assert actor.actor == "shared-session"
    assert actor.source == "CloudTrail sessão SSO"


def test_graph_context_reaches_opportunity():
    analysis = analyze_account(_process_account())
    opportunity = next(
        item for item in analysis.opportunities if item.asset_name == "transforma"
    )
    assert opportunity.downstream_consumers == 5
    assert opportunity.process_criticality == 1.0
    assert opportunity.owner == "Squad Dados"
    assert any("cadeia atende 5" in evidence for evidence in opportunity.evidence)
    assert any("cadeia compartilhada" in risk for risk in opportunity.risks)


class _Paginator:
    def __init__(self, events):
        self.events = events

    def paginate(self, **_):
        yield {"Events": self.events}


class _CloudTrail:
    def __init__(self, events):
        self.events = events

    def get_paginator(self, name):
        assert name == "lookup_events"
        return _Paginator(self.events)


def test_cloudtrail_collector_normalizes_source_identity():
    raw = {
        "requestParameters": {"jobName": "transforma"},
        "userIdentity": {
            "arn": "arn:aws:sts::123:assumed-role/DataRole/ana@empresa.com",
            "sessionContext": {"sourceIdentity": "ana-id"},
        },
    }
    client = _CloudTrail(
        [
            {
                "EventName": "StartJobRun",
                "EventTime": datetime(2026, 7, 22, tzinfo=timezone.utc),
                "CloudTrailEvent": json.dumps(raw),
            }
        ]
    )
    events = collect_actor_events(client, now=datetime(2026, 7, 23, tzinfo=timezone.utc))
    assert len(events) == 1
    assert events[0].resource_type == "glue_job"
    assert events[0].resource_name == "transforma"
    assert events[0].source_identity == "ana-id"
