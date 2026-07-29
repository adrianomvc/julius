"""Taxonomia de cobrança do SageMaker por `USAGE_TYPE`."""

from __future__ import annotations

SAGEMAKER_USAGE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("featurestore", "feature_store"),
    ("feature-store", "feature_store"),
    ("studio:volumeusage", "studio_storage"),
    ("studio-volumeusage", "studio_storage"),
    ("serverless", "serverless"),
    ("processing", "processing"),
    ("training", "training"),
    ("transform", "transform"),
    ("async", "endpoint"),
    ("hosting", "endpoint"),
    ("endpoint", "endpoint"),
    ("studio", "studio"),
    ("notebook", "notebook"),
    ("geospatial", "geospatial"),
)

ALLOCATABLE_SAGEMAKER_BUCKETS = frozenset(
    {
        "studio",
        "studio_storage",
        "notebook",
        "training",
        "processing",
        "transform",
        "endpoint",
        "serverless",
        "feature_store",
    }
)

UNATTRIBUTED_SAGEMAKER_BUCKETS = frozenset({"geospatial", "other"})
