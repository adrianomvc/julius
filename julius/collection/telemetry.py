"""Telemetria do custo operacional do próprio scan.

Contada de várias threads: a listagem completa de S3 e o histórico de execuções
do Glue coletam vários alvos ao mesmo tempo sobre o mesmo cliente instrumentado.

A perda de contagem vinha de `stat()`, não do incremento. `if key not in
api_calls: api_calls[key] = ...` são duas operações: duas threads estreando a
mesma operação criavam dois `ApiCallStat`, e a segunda atribuição descartava o
que a primeira já havia contado. Medido no padrão antigo: 211 chamadas perdidas
em 20 rodadas de 16 threads (CPython 3.14, GIL ligado).

`ApiCallStat.add` é a outra metade. `+=` num atributo é read-modify-write e, em
medição, o GIL o mantém exato na prática — mas "exato por acidente do
escalonador" não é garantia, e some no build sem GIL. O lock é por stat, então
operações diferentes contam sem se esperar.

Subcontar aqui não é detalhe de relatório: é `estimate()` que alimenta o
`--max-scan-cost`, e orçamento apoiado em número subcontado não é orçamento.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from threading import Condition, Lock
from time import perf_counter
from typing import Any

from julius.collection.iam import DeclaredPermissionDenied, action_for_call


class CollectionMemoryLimitExceeded(RuntimeError):
    """Interrompe uma fonte opcional quando o limite explícito foi excedido."""


@dataclass
class ApiCallStat:
    service: str
    operation: str
    calls: int = 0
    pages: int = 0
    retries: int = 0
    throttles: int = 0
    duration_ms: int = 0
    cache_hits: int = 0
    bytes_scanned: int = 0

    def add(self, **deltas: int) -> None:
        """Soma deltas sob o lock do próprio stat.

        Lock por stat, não global: duas operações diferentes contam em paralelo
        sem se esperarem, e o custo dele é irrelevante diante da latência de
        rede que acabou de ser paga.
        """
        with self._lock:
            for name, delta in deltas.items():
                setattr(self, name, getattr(self, name) + delta)

    def __post_init__(self) -> None:
        # Fora do `dataclass` de propósito: um `Lock` não é dado do scan, não
        # entra em comparação nem em serialização do manifesto.
        self._lock = Lock()

    def value(self, name: str) -> int:
        with self._lock:
            return int(getattr(self, name))


@dataclass
class RunTelemetry:
    api_calls: dict[str, ApiCallStat] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    unpriced_operations: list[str] = field(default_factory=list)
    execution_mode: str = "serial"
    collection_wall_ms: int = 0
    source_duration_ms: int = 0
    max_parallel_sources: int = 1
    max_pending_sources: int = 0
    critical_path_ms: int = 0
    critical_path_sources: list[str] = field(default_factory=list)
    scheduler_wait_ms: int = 0
    service_concurrency_limits: dict[str, int] = field(default_factory=dict)
    service_limit_reductions: dict[str, int] = field(default_factory=dict)
    page_concurrency_limit: int = 1
    max_parallel_pages: int = 0
    page_backpressure_wait_ms: int = 0
    peak_memory_bytes: int = 0
    memory_limit_bytes: int = 0
    memory_pressure_events: int = 0
    resumed_sources: int = 0
    superseded_runs: int = 0
    superseded_ai_jobs: int = 0
    ai_queue_depth: int = 0
    ai_queue_oldest_wait_ms: int = 0
    ai_queue_rejected: int = 0
    snapshot_hits: int = 0
    snapshot_misses: int = 0
    cloudwatch_metric_requests: int = 0
    cloudwatch_metric_queries: int = 0
    cloudwatch_metric_batches: int = 0
    cloudwatch_coalesced_requests: int = 0
    cloudwatch_deduplicated_queries: int = 0
    cloudwatch_avoided_calls: int = 0
    cloudwatch_estimated_saved_ms: int = 0
    iam_short_circuits: int = 0

    def __post_init__(self) -> None:
        self._lock = Lock()
        self._page_condition = Condition()
        self._active_pages = 0

    def stat(self, service: str, operation: str) -> ApiCallStat:
        key = f"{service}:{operation}"
        # `if key not in` seguido de atribuição é duas operações: sem o lock,
        # duas threads criavam o mesmo stat e uma sobrescrevia a contagem da
        # outra. O lock só protege a criação; a soma é do lock do stat.
        stat = self.api_calls.get(key)
        if stat is not None:
            return stat
        with self._lock:
            if key not in self.api_calls:
                self.api_calls[key] = ApiCallStat(service=service, operation=operation)
            return self.api_calls[key]

    def throttles(self, service: str) -> int:
        """Snapshot consistente de throttles de um serviço."""
        with self._lock:
            stats = [
                stat for stat in self.api_calls.values() if stat.service == service
            ]
        return sum(stat.value("throttles") for stat in stats)

    def record_snapshot(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self.snapshot_hits += 1
            else:
                self.snapshot_misses += 1

    def record_cloudwatch_batch(
        self,
        *,
        requests: int = 0,
        queries: int = 0,
        batches: int = 0,
        coalesced: int = 0,
        deduplicated: int = 0,
        avoided_calls: int = 0,
        estimated_saved_ms: int = 0,
    ) -> None:
        """Registra economia de lote sem disputar o lock das chamadas API."""
        with self._lock:
            self.cloudwatch_metric_requests += requests
            self.cloudwatch_metric_queries += queries
            self.cloudwatch_metric_batches += batches
            self.cloudwatch_coalesced_requests += coalesced
            self.cloudwatch_deduplicated_queries += deduplicated
            self.cloudwatch_avoided_calls += avoided_calls
            self.cloudwatch_estimated_saved_ms += estimated_saved_ms

    def configure_pressure(
        self, *, page_limit: int, memory_limit_mb: int | None
    ) -> None:
        with self._page_condition:
            self.page_concurrency_limit = max(1, page_limit)
        self.memory_limit_bytes = max(0, int(memory_limit_mb or 0)) * 1024 * 1024

    def acquire_page(self, *, current_memory_bytes: int = 0) -> None:
        """Aplica backpressure entre páginas antes de entregá-las ao coletor."""
        started = perf_counter()
        with self._page_condition:
            self._page_condition.wait_for(
                lambda: self._active_pages < self.page_concurrency_limit
            )
            waited = max(0, round((perf_counter() - started) * 1000))
            self._active_pages += 1
            with self._lock:
                self.page_backpressure_wait_ms += waited
                self.max_parallel_pages = max(
                    self.max_parallel_pages, self._active_pages
                )
                self.peak_memory_bytes = max(
                    self.peak_memory_bytes, current_memory_bytes
                )
                if (
                    self.memory_limit_bytes
                    and current_memory_bytes > self.memory_limit_bytes
                ):
                    self.memory_pressure_events += 1
                    self._active_pages -= 1
                    self._page_condition.notify_all()
                    raise CollectionMemoryLimitExceeded(
                        "limite de memória da coleta excedido entre páginas"
                    )

    def release_page(self) -> None:
        with self._page_condition:
            self._active_pages -= 1
            self._page_condition.notify_all()

    def record_memory_peak(self, peak_bytes: int) -> None:
        with self._lock:
            self.peak_memory_bytes = max(self.peak_memory_bytes, peak_bytes)

    def record_limiter_reduction(self, service: str) -> None:
        with self._lock:
            self.service_limit_reductions[service] = (
                self.service_limit_reductions.get(service, 0) + 1
            )

    def record_scheduler_wait(self, wait_ms: int) -> None:
        with self._lock:
            self.scheduler_wait_ms += max(0, wait_ms)

    def record_iam_short_circuit(self) -> None:
        with self._lock:
            self.iam_short_circuits += 1

    def estimate(self, pricing) -> float:
        cost = 0.0
        unpriced = []
        # Cópia sob o lock: `estimate` roda no gate de orçamento entre fontes, e
        # iterar o dict enquanto uma thread cria um stat novo levantaria
        # RuntimeError — derrubando a coleta por causa da contabilidade dela.
        with self._lock:
            stats = list(self.api_calls.values())
        for stat in stats:
            quantity = stat.pages or stat.calls
            if stat.service == "ce":
                cost += 0.01 * quantity
            elif stat.service == "athena" and stat.operation == "get_query_execution":
                cost += (
                    stat.bytes_scanned / 1024**4
                ) * float(getattr(pricing, "athena_per_tb_usd", 0.0) or 0.0)
            elif stat.service == "s3":
                kind = (
                    "list"
                    if stat.operation.startswith("list_")
                    else "get"
                    if stat.operation.startswith("get_")
                    else ""
                )
                rate = getattr(pricing, "s3_request_per_1000", {}).get(kind)
                if kind and rate is not None:
                    cost += rate * quantity / 1000
                elif kind:
                    unpriced.append(f"{stat.service}:{stat.operation}")
            elif quantity:
                unpriced.append(f"{stat.service}:{stat.operation}")
        self.estimated_cost_usd = round(cost, 6)
        self.unpriced_operations = sorted(set(unpriced))
        return self.estimated_cost_usd


class InstrumentedClient:
    """Proxy transparente que conta chamadas, páginas, retries e cache CE."""

    def __init__(
        self,
        client,
        service: str,
        telemetry: RunTelemetry,
        cache: dict,
        *,
        cache_lock=None,
        denied_iam_actions: frozenset[str] = frozenset(),
    ):
        self._client = client
        self._service = service
        self._telemetry = telemetry
        self._cache = cache
        self._cache_lock = cache_lock or Lock()
        self._started_query_ids: set[str] = set()
        # Preenchido somente no cliente CloudWatch pelo CollectionContext.
        # Fica declarado aqui para o proxy continuar transparente e tipável.
        self._metric_batch_coordinator: Any = None
        self._denied_iam_actions = denied_iam_actions

    def __getattr__(self, name):
        target = getattr(self._client, name)
        if name == "get_paginator":
            return lambda operation: _InstrumentedPaginator(
                target(operation),
                self._service,
                operation,
                self._telemetry,
                self._cache,
                self._cache_lock,
                self._denied_iam_actions,
            )
        if not callable(target):
            return target

        def call(*args, **kwargs):
            stat = self._telemetry.stat(self._service, name)
            action = action_for_call(self._service, name)
            if action in self._denied_iam_actions:
                self._telemetry.record_iam_short_circuit()
                exc = DeclaredPermissionDenied(action)
                _annotate_iam_exception(exc, self._service, name)
                raise exc
            cache_key = _cache_key(self._service, name, args, kwargs)
            if self._service == "ce":
                with self._cache_lock:
                    cached = self._cache.get(cache_key)
                if cached is not None:
                    stat.add(cache_hits=1)
                    return cached
            started = perf_counter()
            try:
                result = target(*args, **kwargs)
            except Exception as exc:
                _annotate_iam_exception(exc, self._service, name)
                code = str(
                    getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                ).lower()
                stat.add(
                    calls=1,
                    duration_ms=round((perf_counter() - started) * 1000),
                    throttles=int("throttl" in code),
                )
                raise
            metadata = result.get("ResponseMetadata", {}) if isinstance(result, dict) else {}
            stat.add(
                calls=1,
                duration_ms=round((perf_counter() - started) * 1000),
                retries=int(metadata.get("RetryAttempts") or 0),
            )
            if name == "start_query_execution" and isinstance(result, dict):
                query_id = str(result.get("QueryExecutionId") or "")
                if query_id:
                    self._started_query_ids.add(query_id)
            if (
                name == "get_query_execution"
                and isinstance(result, dict)
                and str(kwargs.get("QueryExecutionId") or "")
                in self._started_query_ids
            ):
                stat.add(
                    bytes_scanned=int(
                        (
                            result.get("QueryExecution", {}).get("Statistics", {}) or {}
                        ).get("DataScannedInBytes", 0)
                        or 0
                    )
                )
            if self._service == "ce":
                with self._cache_lock:
                    self._cache[cache_key] = result
            return result

        return call


class _InstrumentedPaginator:
    def __init__(
        self,
        paginator,
        service,
        operation,
        telemetry,
        cache,
        cache_lock,
        denied_iam_actions,
    ):
        self._paginator = paginator
        self._service = service
        self._operation = operation
        self._telemetry = telemetry
        self._cache = cache
        self._cache_lock = cache_lock
        self._denied_iam_actions = denied_iam_actions

    def paginate(self, **kwargs):
        import tracemalloc

        stat = self._telemetry.stat(self._service, self._operation)
        action = action_for_call(self._service, self._operation)
        if action in self._denied_iam_actions:
            self._telemetry.record_iam_short_circuit()
            exc = DeclaredPermissionDenied(action)
            _annotate_iam_exception(exc, self._service, self._operation)
            raise exc
        cache_key = _cache_key(self._service, self._operation, (), kwargs)
        if self._service == "ce":
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached is not None:
                stat.add(cache_hits=1)
                yield from cached
                return
        # Somente Cost Explorer usa o cache de resposta. Antes todas as páginas
        # de todos os serviços ficavam retidas até o fim da paginação, anulando
        # o streaming justamente nas contas maiores.
        pages = [] if self._service == "ce" else None
        completed = False
        started = perf_counter()
        try:
            for page in self._paginator.paginate(**kwargs):
                current_memory = (
                    tracemalloc.get_traced_memory()[0]
                    if tracemalloc.is_tracing()
                    else 0
                )
                self._telemetry.acquire_page(current_memory_bytes=current_memory)
                metadata = page.get("ResponseMetadata", {})
                stat.add(
                    calls=1,
                    pages=1,
                    retries=int(metadata.get("RetryAttempts") or 0),
                )
                if pages is not None:
                    pages.append(page)
                try:
                    yield page
                finally:
                    self._telemetry.release_page()
            completed = True
        except Exception as exc:
            _annotate_iam_exception(exc, self._service, self._operation)
            raise
        finally:
            stat.add(duration_ms=round((perf_counter() - started) * 1000))
            if pages is not None and completed:
                with self._cache_lock:
                    self._cache[cache_key] = pages


def _cache_key(service: str, operation: str, args, kwargs) -> str:
    return json.dumps(
        [service, operation, args, kwargs],
        default=str,
        sort_keys=True,
        ensure_ascii=True,
    )


def _annotate_iam_exception(exc: Exception, service: str, operation: str) -> None:
    """Anexa somente identificadores locais; mensagem AWS nunca é persistida."""
    try:
        exc.__dict__["julius_service"] = service
        exc.__dict__["julius_operation"] = operation
    except Exception:  # pragma: no cover - exceção imutável de biblioteca
        return
