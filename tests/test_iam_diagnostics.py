"""IAM estruturado e circuitos somente por declaração explícita."""

from __future__ import annotations

from julius.collection.health import CollectionRecorder
from julius.collection.iam import gaps_from_text
from julius.collection.telemetry import InstrumentedClient, RunTelemetry


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    def get_work_group(self, **_kwargs):
        self.calls += 1
        return {}


def test_text_gaps_become_aggregated_iam_evidence() -> None:
    gaps = gaps_from_text(
        [
            "get_tables[database-a]: permission_denied",
            "get_tables[database-b]: permission_denied",
            "get_tables[database-c]: throttled",
        ]
    )

    assert len(gaps) == 1
    assert gaps[0].iam_action == "glue:GetTables"
    assert gaps[0].affected_resources == 2
    assert gaps[0].examples == ["database-a", "database-b"]


def test_explicit_denial_avoids_network_and_reaches_collection_health() -> None:
    raw = _Client()
    telemetry = RunTelemetry()
    client = InstrumentedClient(
        raw,
        "athena",
        telemetry,
        {},
        denied_iam_actions=frozenset({"athena:GetWorkGroup"}),
    )
    recorder = CollectionRecorder()

    recorder.capture(
        "Athena",
        lambda: client.get_work_group(WorkGroup="legacy"),
        {},
    )

    assert raw.calls == 0
    assert telemetry.iam_short_circuits == 1
    entry = recorder.entries[0]
    assert entry.status == "unavailable"
    assert entry.error_category == "permission_denied"
    assert entry.iam_gaps[0].iam_action == "athena:GetWorkGroup"


def test_without_manifest_the_same_call_reaches_the_client() -> None:
    raw = _Client()
    client = InstrumentedClient(raw, "athena", RunTelemetry(), {})

    client.get_work_group(WorkGroup="legacy")

    assert raw.calls == 1
