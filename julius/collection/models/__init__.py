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
    SageMakerDomain,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerInferenceComponent,
    SageMakerInferenceRecommendation,
    SageMakerJob,
    SageMakerMonitoringSchedule,
    SageMakerNotebook,
    SageMakerPipeline,
    SageMakerSpace,
    SageMakerVariant,
    Schedule,
    StateMachine,
    Table,
)
from julius.collection.models.athena import (
    AthenaActorUsage,
    AthenaCapacityReservation,
    AthenaCoverage,
    AthenaQuery,
)
from julius.collection.models.cost import (
    GlueCostCoverage,
    ProcessCost,
    RedshiftCostCoverage,
    SageMakerCostCoverage,
    SageMakerSavingsPlanCoverage,
    ServiceCost,
)
from julius.collection.models.glue import (
    DataBrewJob,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    InteractiveSession,
)
from julius.collection.models.health import CollectionHealth, IamGap
from julius.collection.models.s3 import (
    LAST_ACCESS_SOURCES,
    PREFIX_KINDS,
    S3Bucket,
    S3BucketConfig,
    S3CostCoverage,
    S3CostLine,
    S3MultipartUpload,
    S3Prefix,
)
from julius.collection.models.window_math import monthly_factor

__all__ = [
    "Account",
    "ActorEvent",
    "AthenaActorUsage",
    "AthenaCapacityReservation",
    "AthenaCoverage",
    "AthenaQuery",
    "CollectionHealth",
    "IamGap",
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
    "SageMakerDomain",
    "SageMakerCostCoverage",
    "SageMakerEndpoint",
    "SageMakerFeatureGroup",
    "SageMakerInferenceComponent",
    "SageMakerInferenceRecommendation",
    "SageMakerJob",
    "SageMakerMonitoringSchedule",
    "SageMakerNotebook",
    "SageMakerPipeline",
    "SageMakerSpace",
    "SageMakerSavingsPlanCoverage",
    "SageMakerVariant",
    "Schedule",
    "ServiceCost",
    "S3Prefix",
    "S3MultipartUpload",
    "S3CostCoverage",
    "S3CostLine",
    "S3Bucket",
    "S3BucketConfig",
    "PREFIX_KINDS",
    "LAST_ACCESS_SOURCES",
    "StateMachine",
    "Table",
    "monthly_factor",
]
