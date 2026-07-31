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
from threading import Lock
from time import perf_counter


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


@dataclass
class RunTelemetry:
    api_calls: dict[str, ApiCallStat] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    unpriced_operations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = Lock()

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

    def __init__(self, client, service: str, telemetry: RunTelemetry, cache: dict):
        self._client = client
        self._service = service
        self._telemetry = telemetry
        self._cache = cache
        self._started_query_ids: set[str] = set()

    def __getattr__(self, name):
        target = getattr(self._client, name)
        if name == "get_paginator":
            return lambda operation: _InstrumentedPaginator(
                target(operation),
                self._service,
                operation,
                self._telemetry,
                self._cache,
            )
        if not callable(target):
            return target

        def call(*args, **kwargs):
            stat = self._telemetry.stat(self._service, name)
            cache_key = _cache_key(self._service, name, args, kwargs)
            if self._service == "ce" and cache_key in self._cache:
                stat.add(cache_hits=1)
                return self._cache[cache_key]
            started = perf_counter()
            try:
                result = target(*args, **kwargs)
            except Exception as exc:
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
                self._cache[cache_key] = result
            return result

        return call


class _InstrumentedPaginator:
    def __init__(self, paginator, service, operation, telemetry, cache):
        self._paginator = paginator
        self._service = service
        self._operation = operation
        self._telemetry = telemetry
        self._cache = cache

    def paginate(self, **kwargs):
        stat = self._telemetry.stat(self._service, self._operation)
        cache_key = _cache_key(self._service, self._operation, (), kwargs)
        if self._service == "ce" and cache_key in self._cache:
            stat.add(cache_hits=1)
            yield from self._cache[cache_key]
            return
        pages = []
        started = perf_counter()
        try:
            for page in self._paginator.paginate(**kwargs):
                metadata = page.get("ResponseMetadata", {})
                stat.add(
                    calls=1,
                    pages=1,
                    retries=int(metadata.get("RetryAttempts") or 0),
                )
                pages.append(page)
                yield page
        finally:
            stat.add(duration_ms=round((perf_counter() - started) * 1000))
            if self._service == "ce":
                self._cache[cache_key] = pages


def _cache_key(service: str, operation: str, args, kwargs) -> str:
    return json.dumps(
        [service, operation, args, kwargs],
        default=str,
        sort_keys=True,
        ensure_ascii=True,
    )
