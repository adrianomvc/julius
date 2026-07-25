"""Telemetria sanitizada de uma fonte de coleta."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CollectionHealth:
    """Resultado sanitizado de uma fonte durante uma coleta read-only."""

    source: str
    status: str = "ok"  # ok | partial | unavailable | error
    required: bool = False
    affects_status: bool = True
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    collected: int = 0
    expected: int | None = None
    coverage: float | None = None
    data_through: str = ""
    error_category: str = ""
    impact: str = ""
    next_action: str = ""
