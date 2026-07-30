"""Telemetria do custo operacional do próprio scan."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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


@dataclass
class RunTelemetry:
    api_calls: dict[str, ApiCallStat] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    unpriced_operations: list[str] = field(default_factory=list)

    def stat(self, service: str, operation: str) -> ApiCallStat:
        key = f"{service}:{operation}"
        if key not in self.api_calls:
            self.api_calls[key] = ApiCallStat(service=service, operation=operation)
        return self.api_calls[key]

    def estimate(self, pricing) -> float:
        cost = 0.0
        unpriced = []
        for stat in self.api_calls.values():
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
                stat.cache_hits += 1
                return self._cache[cache_key]
            started = perf_counter()
            try:
                result = target(*args, **kwargs)
            except Exception as exc:
                stat.calls += 1
                stat.duration_ms += round((perf_counter() - started) * 1000)
                code = str(
                    getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                ).lower()
                stat.throttles += int("throttl" in code)
                raise
            stat.calls += 1
            stat.duration_ms += round((perf_counter() - started) * 1000)
            metadata = result.get("ResponseMetadata", {}) if isinstance(result, dict) else {}
            stat.retries += int(metadata.get("RetryAttempts") or 0)
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
                stat.bytes_scanned += int(
                    (result.get("QueryExecution", {}).get("Statistics", {}) or {}).get(
                        "DataScannedInBytes", 0
                    )
                    or 0
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
            stat.cache_hits += 1
            yield from self._cache[cache_key]
            return
        pages = []
        started = perf_counter()
        try:
            for page in self._paginator.paginate(**kwargs):
                stat.calls += 1
                stat.pages += 1
                metadata = page.get("ResponseMetadata", {})
                stat.retries += int(metadata.get("RetryAttempts") or 0)
                pages.append(page)
                yield page
        finally:
            stat.duration_ms += round((perf_counter() - started) * 1000)
            if self._service == "ce":
                self._cache[cache_key] = pages


def _cache_key(service: str, operation: str, args, kwargs) -> str:
    return json.dumps(
        [service, operation, args, kwargs],
        default=str,
        sort_keys=True,
        ensure_ascii=True,
    )
