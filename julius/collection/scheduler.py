"""Scheduler determinístico das fontes de coleta.

Workers fazem somente I/O e devolvem ``SourceResult``. A aplicação no Account e
na saúde ocorre na thread coordenadora, depois que as dependências declaradas
terminaram. Fontes ainda não provadas como seguras continuam seriais.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Condition, Lock
from time import perf_counter

from julius.collection.health import CollectionRecorder, RequiredCollectionError
from julius.collection.models import CollectionHealth
from julius.collection.session import SOURCE_WORKERS
from julius.collection.sources import (
    CollectionContext,
    Source,
    SourceResult,
    apply_result,
    execute,
    run,
)

_SERVICE_LIMITS = {
    "ce": 1,
    "cloudwatch": 2,
    "s3": 2,
    "glue": 2,
    "athena": 2,
    "sagemaker": 2,
}
_DEFAULT_SERVICE_LIMIT = 2


@dataclass
class _PeakTracker:
    active: int = 0
    peak: int = 0
    lock: Lock = field(default_factory=Lock)

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


def run_sources(
    sources: tuple[Source, ...],
    ctx: CollectionContext,
    recorder: CollectionRecorder,
    *,
    execution: str = "parallel",
    workers: int = SOURCE_WORKERS,
    on_source_applied: Callable[[Source, list[CollectionHealth]], None] | None = None,
) -> None:
    """Executa o DAG em paralelo ou no caminho serial de rollback."""
    if execution not in {"parallel", "serial"}:
        raise ValueError("collection_execution deve ser parallel ou serial")
    _validate(sources)
    # O orçamento é verificado entre fontes. Enquanto não houver reserva de
    # custo por tarefa, iniciar várias ao mesmo tempo poderia ultrapassá-lo.
    actual_mode = (
        "serial"
        if execution == "serial" or workers <= 1 or ctx.max_scan_cost_usd is not None
        else "parallel"
    )
    started = perf_counter()
    if actual_mode == "serial":
        for source in sources:
            before = len(recorder.entries)
            run(source, ctx, recorder)
            if on_source_applied is not None:
                on_source_applied(source, recorder.entries[before:])
        peak = 1
        limits: dict[str, int] = {}
    else:
        peak, limits = _run_parallel(
            sources,
            ctx,
            recorder,
            workers=workers,
            on_source_applied=on_source_applied,
        )

    names = {source.name for source in sources}
    ctx.telemetry.execution_mode = actual_mode
    ctx.telemetry.collection_wall_ms = max(
        0, round((perf_counter() - started) * 1000)
    )
    ctx.telemetry.source_duration_ms = sum(
        entry.duration_ms for entry in recorder.entries if entry.source in names
    )
    ctx.telemetry.max_parallel_sources = peak
    ctx.telemetry.service_concurrency_limits = limits


def _run_parallel(
    sources: tuple[Source, ...],
    ctx: CollectionContext,
    recorder: CollectionRecorder,
    *,
    workers: int,
    on_source_applied: Callable[[Source, list[CollectionHealth]], None] | None,
) -> tuple[int, dict[str, int]]:
    positions = {source.name: index for index, source in enumerate(sources)}
    pending = {source.name: source for source in sources}
    completed: set[str] = set()
    entries: dict[str, list] = {}
    tracker = _PeakTracker()
    limiters = {
        service: _AdaptiveLimiter(
            _SERVICE_LIMITS.get(service, _DEFAULT_SERVICE_LIMIT)
        )
        for source in sources
        for service in source.services
    }

    with ThreadPoolExecutor(max_workers=min(workers, len(sources))) as pool:
        running: dict[Future[SourceResult], Source] = {}
        while pending or running:
            ready = [
                source
                for source in sources
                if source.name in pending and source.depends_on <= completed
            ]
            scheduled = False
            for source in ready:
                if len(running) >= workers:
                    break
                if not source.parallel_safe:
                    continue
                future = pool.submit(
                    _execute_limited, source, ctx, limiters, tracker
                )
                running[future] = source
                pending.pop(source.name)
                scheduled = True

            # Uma fonte que ainda enriquece estado compartilhado ganha uma
            # barreira: nenhuma outra fica em voo durante seu collect/apply.
            unsafe = next((source for source in ready if not source.parallel_safe), None)
            if unsafe is not None and not running:
                pending.pop(unsafe.name)
                tracker.enter()
                try:
                    result = execute(unsafe, ctx)
                except RequiredCollectionError as exc:
                    entries[unsafe.name] = exc.collection_entries
                    raise
                finally:
                    tracker.leave()
                entries[unsafe.name] = apply_result(result)
                if on_source_applied is not None:
                    on_source_applied(unsafe, entries[unsafe.name])
                completed.add(unsafe.name)
                continue

            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: positions[running[item].name]):
                    source = running.pop(future)
                    try:
                        result = future.result()
                    except RequiredCollectionError as exc:
                        entries[source.name] = exc.collection_entries
                        raise
                    entries[source.name] = apply_result(result)
                    if on_source_applied is not None:
                        on_source_applied(source, entries[source.name])
                    completed.add(source.name)
                continue

            if pending and not scheduled:
                blocked = ", ".join(sorted(pending))
                raise RuntimeError(f"DAG de coleta bloqueado: {blocked}")

    for source in sources:
        recorder.entries.extend(entries.get(source.name, []))
    return max(1, tracker.peak), {
        service: limiter.limit for service, limiter in sorted(limiters.items())
    }


def _execute_limited(
    source: Source,
    ctx: CollectionContext,
    limiters: dict[str, _AdaptiveLimiter],
    tracker: _PeakTracker,
) -> SourceResult:
    services = sorted(source.services)
    before = {service: ctx.telemetry.throttles(service) for service in services}
    acquired: list[str] = []
    try:
        for service in sorted(source.services):
            limiters[service].acquire()
            acquired.append(service)
        tracker.enter()
        try:
            return execute(source, ctx)
        finally:
            tracker.leave()
    finally:
        for service in reversed(acquired):
            limiters[service].release(
                throttled=ctx.telemetry.throttles(service) > before[service]
            )


class _AdaptiveLimiter:
    """Janela por serviço: recua no throttle e recupera sem salto."""

    def __init__(self, limit: int) -> None:
        self._maximum = max(1, limit)
        self._limit = self._maximum
        self._active = 0
        self._healthy = 0
        self._condition = Condition()

    @property
    def limit(self) -> int:
        with self._condition:
            return self._limit

    def acquire(self) -> None:
        with self._condition:
            self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1

    def release(self, *, throttled: bool) -> None:
        with self._condition:
            self._active -= 1
            if throttled:
                self._limit = max(1, self._limit // 2)
                self._healthy = 0
            else:
                self._healthy += 1
                if self._healthy >= 4 and self._limit < self._maximum:
                    self._limit += 1
                    self._healthy = 0
            self._condition.notify_all()


def _validate(sources: tuple[Source, ...]) -> None:
    names = [source.name for source in sources]
    if len(names) != len(set(names)):
        raise RuntimeError("nomes de fontes duplicados no DAG")
    known = set(names)
    missing = {
        dependency
        for source in sources
        for dependency in source.depends_on
        if dependency not in known
    }
    if missing:
        raise RuntimeError(f"dependências de fontes inexistentes: {sorted(missing)}")

    remaining = {source.name: set(source.depends_on) for source in sources}
    resolved: set[str] = set()
    while remaining:
        ready = {name for name, deps in remaining.items() if deps <= resolved}
        if not ready:
            raise RuntimeError(f"ciclo no DAG de coleta: {sorted(remaining)}")
        resolved.update(ready)
        for name in ready:
            remaining.pop(name)
