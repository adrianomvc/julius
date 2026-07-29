"""Classificação de instância SageMaker.

O preço por tipo de instância saiu daqui para a tabela da região — era o último
lugar com valor literal em código. Sobrou o que é fato sobre a instância, não
premissa de preço: se ela tem GPU.
"""

from __future__ import annotations

_GPU_FAMILIES = (
    "g3",
    "g4dn",
    "g5",
    "g6",
    "g6e",
    "g7e",
    "p2",
    "p3",
    "p4",
    "p5",
    "p6",
)


def is_gpu_instance(instance_type: str) -> bool:
    return any(f".{family}" in instance_type for family in _GPU_FAMILIES)
