"""Preço aproximado por tipo de instância SageMaker."""

from __future__ import annotations

# Preço aproximado (USD/hora) por tipo de instância SageMaker.
SAGEMAKER_HOURLY: dict[str, float] = {
    "ml.t3.medium": 0.05,
    "ml.t3.large": 0.10,
    "ml.m5.large": 0.12,
    "ml.m5.xlarge": 0.23,
    "ml.m5.2xlarge": 0.46,
    "ml.c5.xlarge": 0.21,
    "ml.g4dn.xlarge": 0.53,   # GPU
    "ml.g5.xlarge": 1.00,     # GPU
    "ml.p3.2xlarge": 4.00,    # GPU
}
SAGEMAKER_HOURLY_DEFAULT = 0.18


def sagemaker_hourly(instance_type: str) -> float:
    return SAGEMAKER_HOURLY.get(instance_type, SAGEMAKER_HOURLY_DEFAULT)


def is_gpu_instance(instance_type: str) -> bool:
    return any(f".{fam}" in instance_type for fam in ("g4dn", "g5", "p3", "p4", "p2", "g6"))
