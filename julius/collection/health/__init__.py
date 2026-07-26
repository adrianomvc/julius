"""Telemetria sanitizada da coleta, por fonte."""

from julius.collection.health.recorder import (
    CollectionRecorder,
    RequiredCollectionError,
    error_category,
)

__all__ = ["CollectionRecorder", "RequiredCollectionError", "error_category"]
