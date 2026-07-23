"""Dificuldade (1–5) — determinística e explicável."""

from __future__ import annotations

DIFFICULTY_LABELS: dict[int, str] = {
    1: "Muito fácil",
    2: "Fácil",
    3: "Média",
    4: "Alta",
    5: "Muito alta",
}


def label(difficulty: int) -> str:
    return DIFFICULTY_LABELS.get(difficulty, "Média")
