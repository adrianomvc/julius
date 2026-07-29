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
    RedshiftCluster,
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
    RedshiftCostCoverage,
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
from julius.collection.models.s3 import (
    LAST_ACCESS_SOURCES,
    PREFIX_KINDS,
    S3Bucket,
    S3BucketConfig,
    S3CostCoverage,
    S3MultipartUpload,
    S3Prefix,
)
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
    "RedshiftCostCoverage",
    "GlueCrawler",
    "GlueJob",
    "GlueTrigger",
    "InteractiveSession",
    "PreviousResult",
    "ProcessCost",
    "ProducerCandidate",
    "RedshiftCluster",
    "SageMakerApp",
    "SageMakerEndpoint",
    "Schedule",
    "ServiceCost",
    "S3Prefix",
    "S3MultipartUpload",
    "S3CostCoverage",
    "S3Bucket",
    "S3BucketConfig",
    "PREFIX_KINDS",
    "LAST_ACCESS_SOURCES",
    "StateMachine",
    "Table",
    "monthly_factor",
]
