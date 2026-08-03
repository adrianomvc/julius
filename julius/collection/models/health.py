"""Telemetria sanitizada de uma fonte de coleta."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IamGap:
    """Permissão read-only negada, agregada sem persistir mensagem AWS."""

    service: str
    operation: str
    iam_action: str
    category: str = "permission_denied"
    affected_resources: int = 0
    examples: list[str] = field(default_factory=list)


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
    result_origin: str = "fresh"  # fresh | cached
    cache_age_seconds: int = 0
    iam_gaps: list[IamGap] = field(default_factory=list)
    error_category: str = ""
    impact: str = ""
    next_action: str = ""
