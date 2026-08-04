"""Backpressure cooperativo entre paginadores da coleta."""

from __future__ import annotations

from threading import Event, Thread
from time import sleep

import pytest

from julius.collection.health.recorder import error_category
from julius.collection.telemetry import (
    CollectionMemoryLimitExceeded,
    RunTelemetry,
)


def test_page_limit_blocks_a_second_consumer_until_release():
    telemetry = RunTelemetry()
    telemetry.configure_pressure(page_limit=1, memory_limit_mb=None)
    telemetry.acquire_page()
    acquired = Event()

    def consume() -> None:
        telemetry.acquire_page()
        acquired.set()
        telemetry.release_page()

    worker = Thread(target=consume)
    worker.start()
    sleep(0.02)
    assert not acquired.is_set()

    telemetry.release_page()
    worker.join(timeout=1)

    assert acquired.is_set()
    assert telemetry.max_parallel_pages == 1
    assert telemetry.page_backpressure_wait_ms >= 10


def test_memory_limit_releases_slot_and_uses_stable_health_category():
    telemetry = RunTelemetry()
    telemetry.configure_pressure(page_limit=1, memory_limit_mb=1)

    with pytest.raises(CollectionMemoryLimitExceeded) as raised:
        telemetry.acquire_page(current_memory_bytes=2 * 1024 * 1024)

    assert error_category(raised.value) == "memory_budget_exceeded"
    assert telemetry.memory_pressure_events == 1
    telemetry.configure_pressure(page_limit=1, memory_limit_mb=None)
    telemetry.acquire_page()
    telemetry.release_page()
