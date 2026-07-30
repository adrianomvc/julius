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
    #: Dias que **esta** fonte mediu. Com teto de retenção por família, a janela
    #: da conta deixa de valer para todas as fontes, e ler cobertura sem saber a
    #: janela leva a comparar número de períodos diferentes.
    window_days: int = 0
    data_through: str = ""
    error_category: str = ""
    impact: str = ""
    next_action: str = ""
