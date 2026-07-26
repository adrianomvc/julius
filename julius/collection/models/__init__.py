"""Inventário normalizado por ativo, dividido por domínio.

Os mesmos dataclasses são preenchidos pelos dois caminhos — coleta ao vivo e
dataset exportado — para que detecção, pontuação e relatório não saibam de qual
vieram.
"""

from julius.collection.models.account import Account
from julius.collection.models.assets import (
    ActorEvent,
    PreviousResult,
    ProducerCandidate,
    SageMakerApp,
    SageMakerEndpoint,
    Schedule,
    StateMachine,
    Table,
)
from julius.collection.models.athena import (
    AthenaActorUsage,
    AthenaCoverage,
    AthenaQuery,
)
from julius.collection.models.cost import (
    GlueCostCoverage,
    ProcessCost,
    ServiceCost,
)
from julius.collection.models.glue import (
    DataBrewJob,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    InteractiveSession,
)
from julius.collection.models.health import CollectionHealth
from julius.collection.models.window_math import monthly_factor

__all__ = [
    "Account",
    "ActorEvent",
    "AthenaActorUsage",
    "AthenaCoverage",
    "AthenaQuery",
    "CollectionHealth",
    "DataBrewJob",
    "GlueCostCoverage",
    "GlueCrawler",
    "GlueJob",
    "GlueTrigger",
    "InteractiveSession",
    "PreviousResult",
    "ProcessCost",
    "ProducerCandidate",
    "SageMakerApp",
    "SageMakerEndpoint",
    "Schedule",
    "ServiceCost",
    "StateMachine",
    "Table",
    "monthly_factor",
]
