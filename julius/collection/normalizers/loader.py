"""Carrega um dataset exportado (JSON) para o inventário normalizado.

No MVP 1A a fonte é um arquivo exportado; os coletores boto3 ao vivo preenchem
os mesmos dataclasses nas fases seguintes.
"""

from __future__ import annotations

import json
from pathlib import Path

from julius.collection.models import (
    Account,
    ActorEvent,
    AthenaActorUsage,
    AthenaCapacityReservation,
    AthenaCoverage,
    AthenaQuery,
    CollectionHealth,
    DataBrewJob,
    GlueCostCoverage,
    GlueCrawler,
    GlueJob,
    GlueTrigger,
    InteractiveSession,
    PreviousResult,
    ProcessCost,
    ProducerCandidate,
    RedshiftCluster,
    RedshiftCostCoverage,
    S3Bucket,
    S3BucketConfig,
    S3CostCoverage,
    S3CostLine,
    S3MultipartUpload,
    S3Prefix,
    SageMakerApp,
    SageMakerCostCoverage,
    SageMakerDomain,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerInferenceComponent,
    SageMakerInferenceRecommendation,
    SageMakerJob,
    SageMakerMonitoringSchedule,
    SageMakerNotebook,
    SageMakerPipeline,
    SageMakerSavingsPlanCoverage,
    SageMakerSpace,
    SageMakerVariant,
    Schedule,
    ServiceCost,
    StateMachine,
    Table,
)
from julius.collection.settings import ANALYSIS_WINDOW_DAYS, DATASET_SCHEMA_VERSION
from julius.collection.telemetry import ApiCallStat, RunTelemetry


def _pick(d: dict, cls):
    """Instancia `cls` apenas com as chaves que ela conhece (tolerante a extras)."""
    fields = set(getattr(cls, "__dataclass_fields__", {}))
    return cls(**{k: v for k, v in d.items() if k in fields})


def _sagemaker_endpoint(raw: dict) -> SageMakerEndpoint:
    """Carrega os novos filhos sem quebrar o formato plano anterior."""
    values = dict(raw)
    values["variants"] = [
        _pick(item, SageMakerVariant) for item in raw.get("variants", [])
    ]
    values["inference_components"] = [
        _pick(item, SageMakerInferenceComponent)
        for item in raw.get("inference_components", [])
    ]
    return _pick(values, SageMakerEndpoint)


def _usd_record(raw: dict) -> dict | None:
    """Aceita o registro só quando ele está declaradamente em USD.

    A AWS reporta custo em USD, então todo dataset gerado pelo `julius collect`
    já vem em USD. Um registro sem `currency` é anterior a esse contrato e um
    registro em outra moeda não pode ser reexpresso sem câmbio — em ambos os
    casos ele é recusado, para não virar número sem procedência no relatório.
    """
    declared = str(raw.get("currency") or "").upper()
    if declared != "USD":
        return None
    return dict(raw)


def _service_cost(raw: dict) -> ServiceCost | None:
    normalized = _usd_record(raw)
    if normalized is None:
        return None
    normalized.setdefault("period_kind", "monthly")
    return _pick(normalized, ServiceCost)


def _previous_result(raw: dict) -> PreviousResult | None:
    normalized = _usd_record(raw)
    if normalized is None:
        return None
    return _pick(normalized, PreviousResult)


class UnsupportedDatasetVersionError(RuntimeError):
    """O dataset é de um esquema anterior e não pode ser reinterpretado.

    Os campos de consumo mudaram de significado — antes mês-corrente, agora
    janela móvel de dias completos. `_pick` descartaria os nomes antigos em
    silêncio e o consumo viraria zero, então a recusa é explícita, como já
    acontece com um registro que chega fora de USD.
    """

    def __init__(self, found: int):
        self.found = found
        super().__init__(
            f"dataset no esquema {found}; esperado {DATASET_SCHEMA_VERSION}. "
            "Regenere com `julius collect` — os campos de consumo mudaram de "
            "mês-corrente para janela de dias completos e não são conversíveis."
        )


def load_account(path: str | Path) -> Account:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(raw.get("dataset_schema_version", 1))
    if version != DATASET_SCHEMA_VERSION:
        raise UnsupportedDatasetVersionError(version)
    window = raw.get("window") or {}
    scope = raw.get("scope") or {}
    account = Account(
        account_id=raw["account"],
        # Dataset anterior à política mantém a cobertura histórica completa.
        scope_profile=str(scope.get("profile") or "full_analysis"),
        s3_mode=str(scope.get("s3_mode") or "proposal"),
        region=raw.get("region", "sa-east-1"),
        period=raw.get("period", ""),
        lookback_days=raw.get("lookback_days", ANALYSIS_WINDOW_DAYS),
        generated_at=raw.get("generated_at", ""),
        window_start=str(window.get("start") or ""),
        window_end=str(window.get("end") or ""),
        window_days=int(window.get("days") or ANALYSIS_WINDOW_DAYS),
        # USD é a única moeda aceita; a AWS já reporta custo em USD.
        currency="USD",
    )
    account.collection_health = [
        _pick(item, CollectionHealth)
        for item in raw.get("collection_health", [])
    ]
    ce = raw.get("cost_explorer", {})
    account.services = [
        service
        for service in (_service_cost(s) for s in ce.get("services", []))
        if service is not None
    ]
    account.glue_jobs = [_pick(j, GlueJob) for j in raw.get("glue_jobs", [])]
    account.interactive_sessions = [
        _pick(s, InteractiveSession) for s in raw.get("interactive_sessions", [])
    ]
    account.glue_crawlers = [
        _pick(c, GlueCrawler) for c in raw.get("glue_crawlers", [])
    ]
    account.glue_triggers = [
        _pick(t, GlueTrigger) for t in raw.get("glue_triggers", [])
    ]
    account.databrew_jobs = [
        _pick(j, DataBrewJob) for j in raw.get("databrew_jobs", [])
    ]
    account.process_costs = [
        _pick(p, ProcessCost) for p in raw.get("process_costs", [])
    ]
    account.athena_queries = [_pick(q, AthenaQuery) for q in raw.get("athena_queries", [])]
    account.athena_capacity_reservations = [
        _pick(r, AthenaCapacityReservation)
        for r in raw.get("athena_capacity_reservations", [])
    ]
    telemetry = raw.get("run_telemetry") or {}
    account.run_telemetry = RunTelemetry(
        api_calls={
            key: _pick(value, ApiCallStat)
            for key, value in (telemetry.get("api_calls") or {}).items()
        },
        estimated_cost_usd=float(telemetry.get("estimated_cost_usd") or 0),
        unpriced_operations=list(telemetry.get("unpriced_operations") or []),
    )
    if raw.get("athena_coverage"):
        account.athena_coverage = _pick(raw["athena_coverage"], AthenaCoverage)
    if raw.get("glue_cost_coverage"):
        account.glue_cost_coverage = _pick(
            raw["glue_cost_coverage"], GlueCostCoverage
        )
    account.athena_actor_usage = [
        _pick(a, AthenaActorUsage) for a in raw.get("athena_actor_usage", [])
    ]
    account.state_machines = [_pick(s, StateMachine) for s in raw.get("state_machines", [])]
    stepfunctions_operational = raw.get("stepfunctions_operational") or {}
    account.stepfunctions_map_backlog = int(
        stepfunctions_operational.get("map_backlog") or 0
    )
    account.stepfunctions_open_executions = int(
        stepfunctions_operational.get("open_executions") or 0
    )
    account.stepfunctions_service_integration_failures = int(
        stepfunctions_operational.get("service_integration_failures") or 0
    )
    account.stepfunctions_service_integration_timeouts = int(
        stepfunctions_operational.get("service_integration_timeouts") or 0
    )
    account.sagemaker_apps = [_pick(a, SageMakerApp) for a in raw.get("sagemaker_apps", [])]
    account.sagemaker_spaces = [
        _pick(s, SageMakerSpace) for s in raw.get("sagemaker_spaces", [])
    ]
    account.sagemaker_domains = [
        _pick(d, SageMakerDomain) for d in raw.get("sagemaker_domains", [])
    ]
    account.sagemaker_endpoints = [
        _sagemaker_endpoint(e) for e in raw.get("sagemaker_endpoints", [])
    ]
    account.sagemaker_notebooks = [
        _pick(n, SageMakerNotebook) for n in raw.get("sagemaker_notebooks", [])
    ]
    account.sagemaker_jobs = [
        _pick(j, SageMakerJob) for j in raw.get("sagemaker_jobs", [])
    ]
    account.sagemaker_feature_groups = [
        _pick(g, SageMakerFeatureGroup)
        for g in raw.get("sagemaker_feature_groups", [])
    ]
    account.sagemaker_pipelines = [
        _pick(p, SageMakerPipeline) for p in raw.get("sagemaker_pipelines", [])
    ]
    account.sagemaker_monitoring_schedules = [
        _pick(s, SageMakerMonitoringSchedule)
        for s in raw.get("sagemaker_monitoring_schedules", [])
    ]
    account.sagemaker_inference_recommendations = [
        _pick(r, SageMakerInferenceRecommendation)
        for r in raw.get("sagemaker_inference_recommendations", [])
    ]
    if raw.get("sagemaker_cost_coverage"):
        account.sagemaker_cost_coverage = _pick(
            raw["sagemaker_cost_coverage"], SageMakerCostCoverage
        )
    if raw.get("sagemaker_savings_plans"):
        account.sagemaker_savings_plans = _pick(
            raw["sagemaker_savings_plans"], SageMakerSavingsPlanCoverage
        )
    account.redshift_clusters = [
        _pick(c, RedshiftCluster) for c in raw.get("redshift_clusters", [])
    ]
    if raw.get("redshift_cost_coverage"):
        account.redshift_cost_coverage = _pick(
            raw["redshift_cost_coverage"], RedshiftCostCoverage
        )
    account.s3_buckets = [_pick(b, S3Bucket) for b in raw.get("s3_buckets", [])]
    account.s3_prefixes = [_pick(x, S3Prefix) for x in raw.get("s3_prefixes", [])]
    account.s3_multipart = [
        _pick(m, S3MultipartUpload) for m in raw.get("s3_multipart", [])
    ]
    account.s3_bucket_configs = [
        _pick(c, S3BucketConfig) for c in raw.get("s3_bucket_configs", [])
    ]
    if raw.get("s3_cost_coverage"):
        coverage_raw = dict(raw["s3_cost_coverage"])
        coverage_raw["lines"] = [
            _pick(line, S3CostLine) for line in coverage_raw.get("lines", [])
        ]
        account.s3_cost_coverage = _pick(coverage_raw, S3CostCoverage)
    account.tables = [_pick(t, Table) for t in raw.get("tables", [])]
    account.schedules = [_pick(s, Schedule) for s in raw.get("schedules", [])]
    account.actor_events = [_pick(e, ActorEvent) for e in raw.get("actor_events", [])]
    gov = raw.get("governance", {})
    account.producer_candidates = [
        _pick(p, ProducerCandidate) for p in gov.get("producer_candidates", [])
    ]
    account.previous_results = [
        result
        for result in (_previous_result(r) for r in gov.get("previous_results", []))
        if result is not None
    ]
    return account
