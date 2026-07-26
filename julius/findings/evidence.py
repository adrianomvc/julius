"""Com que evidência um achado se sustenta — e o que falta nela."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Evidence:
    """O que foi observado e quanto disso a coleta realmente cobriu.

    `has_optional_metrics` é o gate que separa recomendação de investigação:
    sem a métrica complementar, a regra pode apontar o sintoma mas não afirmar
    a economia.
    """

    items: Sequence[str] = field(default_factory=tuple)
    sources: Sequence[str] = field(default_factory=tuple)
    observed_runs: int = 0
    coverage_days: int = 0
    has_optional_metrics: bool = False
    owner_tag: str | None = None
