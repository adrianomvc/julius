"""O que fazer a respeito de um achado, e o que isso custa em risco."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Recommendation:
    """A ação proposta, como aplicá-la e como confirmar que funcionou.

    `blocked` marca a recomendação que existe mas ainda não pode ser executada
    — evidência insuficiente, benchmark pendente. Uma investigação bloqueada
    aparece no relatório e não reserva saldo financeiro.
    """

    action: str
    how_to_apply: str
    how_to_validate: str
    difficulty: int
    risks: Sequence[str] = field(default_factory=tuple)
    docs: Sequence[str] = field(default_factory=tuple)
    # Risco relativo usado na priorização; 0.6 é o neutro do modelo.
    risk: float = 0.6
    blocked: bool = False
