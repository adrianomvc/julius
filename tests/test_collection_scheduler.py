"""Contrato do DAG de coleta: rápido sem mudar o resultado."""

from __future__ import annotations

import json
from threading import Lock
from time import sleep

import pytest

from julius.collection.health import CollectionRecorder
from julius.collection.models import Account
from julius.collection.scheduler import _AdaptiveLimiter, run_sources
from julius.collection.sources import (
    CollectionContext,
    Source,
    apply_result,
    execute,
)
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG


class _Session:
    region_name = "sa-east-1"

    def client(self, _service, **_kwargs):
        return object()


def _context(account: Account | None = None, **kwargs) -> CollectionContext:
    return CollectionContext(
        session=_Session(),
        window=AnalysisWindow.trailing(),
        billing=BillingMonth.current(),
        account=account or Account(account_id="123456789012"),
        config=DEFAULT_CONFIG,
        **kwargs,
    )


def _source(
    name: str,
    collect,
    *,
    into: str,
    depends_on: frozenset[str] = frozenset(),
    parallel_safe: bool = True,
) -> Source:
    return Source(
        name=name,
        collect=collect,
        into=into,
        count=len,
        impact="fonte indisponível",
        next_action="validar leitura",
        depends_on=depends_on,
        parallel_safe=parallel_safe,
    )


def test_execute_does_not_publish_inside_the_worker():
    context = _context()
    source = _source("A", lambda _ctx: ["machine"], into="state_machines")

    result = execute(source, context)

    assert context.account.state_machines == []
    apply_result(result)
    assert context.account.state_machines == ["machine"]


def test_independent_sources_overlap_and_health_keeps_registry_order():
    active = 0
    peak = 0
    lock = Lock()

    def collect(value: str, delay: float):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            sleep(delay)
            return [value]
        finally:
            with lock:
                active -= 1

    sources = (
        _source("lenta", lambda _ctx: collect("machine", 0.04), into="state_machines"),
        _source("rápida", lambda _ctx: collect("schedule", 0.005), into="schedules"),
    )
    context = _context()
    recorder = CollectionRecorder()

    run_sources(sources, context, recorder, workers=2)

    assert peak > 1
    assert context.telemetry.max_parallel_sources > 1
    assert context.account.state_machines == ["machine"]
    assert context.account.schedules == ["schedule"]
    assert [entry.source for entry in recorder.entries] == ["lenta", "rápida"]


def test_source_callback_runs_after_value_is_applied():
    context = _context()
    recorder = CollectionRecorder()
    seen: list[tuple[str, list[str], str]] = []
    source = _source(
        "machine",
        lambda _ctx: ["applied"],
        into="state_machines",
    )

    run_sources(
        (source,),
        context,
        recorder,
        on_source_applied=lambda item, entries: seen.append(
            (item.name, list(context.account.state_machines), entries[0].status)
        ),
    )

    assert seen == [("machine", ["applied"], "ok")]


def test_dependency_is_applied_before_dependent_source_starts():
    sources = (
        _source("inventário", lambda _ctx: ["machine"], into="state_machines"),
        _source(
            "dependente",
            lambda ctx: [f"schedule-for-{ctx.account.state_machines[0]}"],
            into="schedules",
            depends_on=frozenset({"inventário"}),
        ),
    )
    context = _context()

    run_sources(sources, context, CollectionRecorder(), workers=2)

    assert context.account.schedules == ["schedule-for-machine"]


def test_resumed_source_satisfies_dependency_without_new_aws_work():
    calls: list[str] = []
    sources = (
        _source(
            "inventário",
            lambda _ctx: calls.append("inventário") or ["new"],
            into="state_machines",
        ),
        _source(
            "dependente",
            lambda ctx: calls.append("dependente")
            or [f"schedule-for-{ctx.account.state_machines[0]}"],
            into="schedules",
            depends_on=frozenset({"inventário"}),
        ),
    )
    account = Account(account_id="123456789012", state_machines=["resumed"])
    context = _context(account)

    run_sources(
        sources,
        context,
        CollectionRecorder(),
        completed_sources=frozenset({"inventário"}),
    )

    assert calls == ["dependente"]
    assert account.schedules == ["schedule-for-resumed"]


def test_serial_mode_is_the_semantic_rollback():
    sources = (
        _source("inventário", lambda _ctx: ["machine"], into="state_machines"),
        _source("agenda", lambda _ctx: ["schedule"], into="schedules"),
    )
    parallel = _context()
    serial = _context()

    run_sources(sources, parallel, CollectionRecorder(), execution="parallel")
    run_sources(sources, serial, CollectionRecorder(), execution="serial")

    assert parallel.account.state_machines == serial.account.state_machines
    assert parallel.account.schedules == serial.account.schedules
    assert parallel.telemetry.execution_mode == "parallel"
    assert serial.telemetry.execution_mode == "serial"


def test_scan_budget_forces_serial_gate_until_reservations_exist():
    sources = (
        _source("A", lambda _ctx: ["machine"], into="state_machines"),
        _source("B", lambda _ctx: ["schedule"], into="schedules"),
    )
    context = _context(max_scan_cost_usd=100.0)

    run_sources(sources, context, CollectionRecorder(), execution="parallel")

    assert context.telemetry.execution_mode == "serial"
    assert context.telemetry.max_parallel_sources == 1


def test_cycle_and_unknown_dependency_fail_before_any_collection():
    cycle = (
        _source(
            "A",
            lambda _ctx: ["a"],
            into="state_machines",
            depends_on=frozenset({"B"}),
        ),
        _source(
            "B",
            lambda _ctx: ["b"],
            into="schedules",
            depends_on=frozenset({"A"}),
        ),
    )
    with pytest.raises(RuntimeError, match="ciclo"):
        run_sources(cycle, _context(), CollectionRecorder())

    unknown = (
        _source(
            "A",
            lambda _ctx: ["a"],
            into="state_machines",
            depends_on=frozenset({"ausente"}),
        ),
    )
    with pytest.raises(RuntimeError, match="inexistentes"):
        run_sources(unknown, _context(), CollectionRecorder())


def test_scheduler_telemetry_survives_dataset_roundtrip(tmp_path):
    from julius.collection.normalizers.dump import account_to_dataset
    from julius.collection.normalizers.loader import load_account

    account = Account(account_id="123456789012")
    account.run_telemetry.execution_mode = "parallel"
    account.run_telemetry.collection_wall_ms = 1200
    account.run_telemetry.source_duration_ms = 3100
    account.run_telemetry.max_parallel_sources = 4
    account.run_telemetry.service_concurrency_limits = {"s3": 1, "glue": 2}
    path = tmp_path / "account.json"
    path.write_text(json.dumps(account_to_dataset(account)), encoding="utf-8")

    loaded = load_account(path)

    assert loaded.run_telemetry.execution_mode == "parallel"
    assert loaded.run_telemetry.collection_wall_ms == 1200
    assert loaded.run_telemetry.source_duration_ms == 3100
    assert loaded.run_telemetry.max_parallel_sources == 4
    assert loaded.run_telemetry.service_concurrency_limits == {"s3": 1, "glue": 2}


def test_service_limiter_backs_off_and_recovers_gradually():
    limiter = _AdaptiveLimiter(4)

    limiter.acquire()
    limiter.release(throttled=True)
    assert limiter.limit == 2

    for _ in range(4):
        limiter.acquire()
        limiter.release(throttled=False)
    assert limiter.limit == 3


def test_throttling_one_service_does_not_reduce_another():
    s3 = _AdaptiveLimiter(4)
    glue = _AdaptiveLimiter(4)

    s3.acquire()
    s3.release(throttled=True)

    assert s3.limit == 2
    assert glue.limit == 4
