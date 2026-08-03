"""Cache local honesto para fontes explicitamente elegíveis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from julius.collection.health import CollectionRecorder
from julius.collection.models import Account
from julius.collection.snapshot import CollectionSnapshotStore, SnapshotPolicy
from julius.collection.sources import (
    CollectionContext,
    Source,
    apply_result,
    execute,
)
from julius.collection.window import AnalysisWindow, BillingMonth


def _policy(*, ttl: int = 60, version: str = "v1") -> SnapshotPolicy:
    return SnapshotPolicy(
        ttl_seconds=ttl,
        collector_version=version,
        serialize=lambda value: value,
        deserialize=lambda value: value,
        scope=lambda ctx: {"profile": ctx.account.scope_profile},
    )


def test_snapshot_validates_ttl_version_scope_checksum_and_account(tmp_path: Path) -> None:
    instant = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    now = [instant]
    store = CollectionSnapshotStore(tmp_path, now=lambda: now[0])
    policy = _policy()
    arguments = {
        "account_id": "111111111111",
        "region": "sa-east-1",
        "source": "config",
        "scope": {"buckets": ["a"]},
        "policy": policy,
    }

    path = store.save(**arguments, value=[{"enabled": True}])
    hit = store.load(**arguments)
    assert hit is not None
    assert hit.value == [{"enabled": True}]
    assert hit.age_seconds == 0
    assert store.load(**{**arguments, "account_id": "222222222222"}) is None
    assert store.load(**{**arguments, "scope": {"buckets": ["b"]}}) is None
    assert store.load(**{**arguments, "policy": _policy(version="v2")}) is None

    now[0] += timedelta(seconds=61)
    assert store.load(**arguments) is None

    path.write_text(path.read_text(encoding="utf-8").replace("true", "false"))
    now[0] = instant
    assert store.load(**arguments) is None


def test_source_reuses_snapshot_and_marks_health_as_cached(tmp_path: Path) -> None:
    instant = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    calls = [0]
    now = [instant]

    def collect(_ctx):
        calls[0] += 1
        return ["item"]

    source = Source(
        name="Stable inventory",
        collect=collect,
        into="schedules",
        count=len,
        impact="inventário ausente",
        next_action="validar permissão",
        snapshot_policy=_policy(),
    )
    store = CollectionSnapshotStore(tmp_path, now=lambda: now[0])

    first = _context(store, instant)
    recorder = CollectionRecorder()
    recorder.entries.extend(apply_result(execute(source, first)))
    assert calls == [1]
    assert first.telemetry.snapshot_misses == 1

    now[0] = instant + timedelta(seconds=20)
    second = _context(store, now[0])
    recorder = CollectionRecorder()
    recorder.entries.extend(apply_result(execute(source, second)))

    assert calls == [1]
    assert second.account.schedules == ["item"]
    assert recorder.entries[0].result_origin == "cached"
    assert recorder.entries[0].cache_age_seconds == 20
    assert second.telemetry.snapshot_hits == 1


def test_partial_source_is_never_saved(tmp_path: Path) -> None:
    instant = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)

    def collect(ctx):
        ctx.gaps.append("bucket-a: permission_denied")
        return ["partial"]

    source = Source(
        name="Partial inventory",
        collect=collect,
        into="schedules",
        count=len,
        impact="inventário parcial",
        next_action="validar permissão",
        snapshot_policy=_policy(),
    )
    store = CollectionSnapshotStore(tmp_path, now=lambda: instant)
    context = _context(store, instant)

    entries = apply_result(execute(source, context))

    assert entries[0].status == "partial"
    assert store.load(
        account_id=context.account.account_id,
        region=context.account.region,
        source=source.name,
        scope=source.snapshot_policy.scope(context),
        policy=source.snapshot_policy,
    ) is None


def _context(
    store: CollectionSnapshotStore, instant: datetime
) -> CollectionContext:
    return CollectionContext(
        session=SimpleNamespace(),
        window=AnalysisWindow.trailing(days=30, now=instant),
        billing=BillingMonth.current(now=instant),
        account=Account(account_id="111111111111", region="sa-east-1"),
        config=SimpleNamespace(pricing=SimpleNamespace()),
        snapshot_store=store,
    )
