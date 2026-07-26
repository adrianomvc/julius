"""Provedores de análise contextual, intercambiáveis por contrato."""

from julius.analysis.providers.base import AnalysisProvider, Workspace
from julius.analysis.providers.devin import DevinProvider
from julius.analysis.providers.manual_file import ManualFileProvider

#: Nome → provedor, para o CLI escolher sem conhecer as classes.
PROVIDERS: dict[str, type[AnalysisProvider]] = {
    "devin": DevinProvider,
    "manual": ManualFileProvider,
}

__all__ = [
    "PROVIDERS",
    "AnalysisProvider",
    "DevinProvider",
    "ManualFileProvider",
    "Workspace",
]
