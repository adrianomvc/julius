"""Inventário read-only dos componentes SageMaker além de Apps e Endpoints."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from statistics import quantiles
from typing import Any

from julius.collection.collectors import metrics
from julius.collection.collectors.metrics import MetricQuery
from julius.collection.collectors.paginate import safe_call, safe_pages
from julius.collection.models import (
    SageMakerDomain,
    SageMakerFeatureGroup,
    SageMakerInferenceRecommendation,
    SageMakerJob,
    SageMakerMonitoringSchedule,
    SageMakerNotebook,
    SageMakerPipeline,
    SageMakerSpace,
)
from julius.collection.ownership_tags import owner_from_tags
from julius.collection.session import SAGEMAKER_DETAIL_WORKERS
from julius.collection.window import AnalysisWindow

_JOB_SPECS = {
    "training": {
        "list": "list_training_jobs",
        "key": "TrainingJobSummaries",
        "name": "TrainingJobName",
        "arn": "TrainingJobArn",
        "status": "TrainingJobStatus",
        "describe": "describe_training_job",
        "arg": "TrainingJobName",
        "namespace": "/aws/sagemaker/TrainingJobs",
    },
    "processing": {
        "list": "list_processing_jobs",
        "key": "ProcessingJobSummaries",
        "name": "ProcessingJobName",
        "arn": "ProcessingJobArn",
        "status": "ProcessingJobStatus",
        "describe": "describe_processing_job",
        "arg": "ProcessingJobName",
        "namespace": "/aws/sagemaker/ProcessingJobs",
    },
    "transform": {
        "list": "list_transform_jobs",
        "key": "TransformJobSummaries",
        "name": "TransformJobName",
        "arn": "TransformJobArn",
        "status": "TransformJobStatus",
        "describe": "describe_transform_job",
        "arg": "TransformJobName",
        "namespace": "/aws/sagemaker/TransformJobs",
    },
}


def collect_spaces(
    sagemaker_client,
    *,
    apps: list,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerSpace]:
    listed = safe_pages(sagemaker_client, "list_spaces", "Spaces")
    _gap(listed, "list_spaces", gaps)
    spaces: list[SageMakerSpace] = []
    for summary in listed.items:
        name = str(summary.get("SpaceName") or "")
        domain_id = str(summary.get("DomainId") or "")
        if not name or not domain_id:
            continue
        described, error = safe_call(
            sagemaker_client,
            "describe_space",
            DomainId=domain_id,
            SpaceName=name,
        )
        if error:
            _append_gap(gaps, f"describe_space: {error}")
        raw = {**summary, **described}
        settings = raw.get("SpaceSettings") or raw.get("SpaceSettingsSummary") or {}
        storage = settings.get("SpaceStorageSettings") or {}
        ebs = storage.get("EbsStorageSettings") or {}
        associated = [
            app
            for app in apps
            if app.domain_id == domain_id and app.space_name == name
        ]
        ownership = raw.get("OwnershipSettings") or summary.get(
            "OwnershipSettingsSummary"
        ) or {}
        spaces.append(
            SageMakerSpace(
                name=name,
                domain_id=domain_id,
                arn=str(raw.get("SpaceArn") or ""),
                status=str(raw.get("Status") or ""),
                sharing_type=str(
                    (raw.get("SpaceSharingSettings") or {}).get("SharingType")
                    or ""
                ),
                owner_tag=_owner(sagemaker_client, raw),
                owner_user_profile=str(
                    ownership.get("OwnerUserProfileName") or ""
                ),
                ebs_volume_size_gb=int(ebs.get("EbsVolumeSizeInGb") or 0),
                created_at=_iso(raw.get("CreationTime")),
                last_modified_at=_iso(raw.get("LastModifiedTime")),
                app_count=len(associated),
                active_app_count=sum(
                    app.status in {"InService", "Pending", "Updating"}
                    for app in associated
                ),
                coverage_days=window.days,
            )
        )
    return spaces


def collect_domains(
    sagemaker_client,
    cloudwatch_client=None,
    *,
    spaces: list[SageMakerSpace],
    apps: list,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerDomain]:
    listed = safe_pages(sagemaker_client, "list_domains", "Domains")
    _gap(listed, "list_domains", gaps)
    domains: list[SageMakerDomain] = []
    for summary in listed.items:
        domain_id = str(summary.get("DomainId") or "")
        if not domain_id:
            continue
        described, error = safe_call(
            sagemaker_client, "describe_domain", DomainId=domain_id
        )
        if error:
            _append_gap(gaps, f"describe_domain: {error}")
        raw = {**summary, **described}
        efs_id = str(raw.get("HomeEfsFileSystemId") or "")
        domain = SageMakerDomain(
            domain_id=domain_id,
            name=str(raw.get("DomainName") or ""),
            arn=str(raw.get("DomainArn") or ""),
            status=str(raw.get("Status") or ""),
            home_efs_file_system_id=efs_id,
            created_at=_iso(raw.get("CreationTime")),
            last_modified_at=_iso(raw.get("LastModifiedTime")),
            owner_tag=_owner(
                sagemaker_client,
                {"ResourceArn": str(raw.get("DomainArn") or "")},
            ),
            space_count=sum(space.domain_id == domain_id for space in spaces),
            active_app_count=sum(
                app.domain_id == domain_id
                and app.status in {"InService", "Pending", "Updating"}
                for app in apps
            ),
            coverage_days=window.days,
        )
        domains.append(domain)
    _apply_efs_metrics(cloudwatch_client, domains, window)
    return domains


_EFS_METRICS = {
    "StorageBytes": ("efs_storage_bytes", "Average"),
    "TotalIOBytes": ("efs_total_io_bytes", "Sum"),
    "DataReadIOBytes": ("efs_read_io_bytes", "Sum"),
    "DataWriteIOBytes": ("efs_write_io_bytes", "Sum"),
    "ClientConnections": ("efs_client_connections", "Sum"),
}


def _apply_efs_metrics(
    cloudwatch_client, domains: list[SageMakerDomain], window
) -> None:
    """Cinco métricas de EFS de todos os domains, em lote.

    Eram cinco chamadas por domain, em série. Só entram os domains com EFS
    identificado: sem `FileSystemId` não há o que perguntar.
    """
    alvos = [
        domain
        for domain in domains
        if domain.home_efs_file_system_id
    ]
    if cloudwatch_client is None or not alvos:
        return
    pedidos = [
        (domain, field_name, metric, MetricQuery(
            namespace="AWS/EFS",
            metric_name=metric,
            stat=statistic,
            dimensions=(
                ("FileSystemId", domain.home_efs_file_system_id),
                *(
                    (("StorageClass", "Total"),)
                    if metric == "StorageBytes"
                    else ()
                ),
            ),
        ))
        for domain in alvos
        for metric, (field_name, statistic) in _EFS_METRICS.items()
    ]
    metrics.collect(
        cloudwatch_client,
        [query for *_resto, query in pedidos],
        start=window.start,
        end=window.end,
    )

    for domain, field_name, metric, query in pedidos:
        if query.values:
            setattr(
                domain,
                field_name,
                max(query.values) if metric == "StorageBytes" else sum(query.values),
            )


def collect_notebooks(
    sagemaker_client,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerNotebook]:
    result = safe_pages(
        sagemaker_client, "list_notebook_instances", "NotebookInstances"
    )
    _gap(result, "list_notebook_instances", gaps)
    notebooks: list[SageMakerNotebook] = []
    for summary in result.items:
        name = str(summary.get("NotebookInstanceName") or "")
        if not name:
            continue
        described, error = safe_call(
            sagemaker_client,
            "describe_notebook_instance",
            NotebookInstanceName=name,
        )
        if error:
            _append_gap(gaps, f"describe_notebook_instance: {error}")
        merged = {**summary, **described}
        notebooks.append(
            SageMakerNotebook(
                name=name,
                arn=str(merged.get("NotebookInstanceArn") or ""),
                status=str(merged.get("NotebookInstanceStatus") or ""),
                instance_type=str(merged.get("InstanceType") or ""),
                platform_identifier=str(merged.get("PlatformIdentifier") or ""),
                lifecycle_config_name=str(
                    merged.get("NotebookInstanceLifecycleConfigName") or ""
                ),
                created_at=_iso(merged.get("CreationTime")),
                last_modified_at=_iso(merged.get("LastModifiedTime")),
                coverage_days=window.days,
                owner_tag=_owner(sagemaker_client, merged),
            )
        )
    return notebooks


def collect_jobs(
    sagemaker_client,
    cloudwatch_client=None,
    *,
    window: AnalysisWindow,
    pricing: Any = None,
    detailed_limit: int = 100,
    full_metrics: bool = False,
    history_days: int = 90,
    low_utilization_threshold: float = 0.30,
    gaps: list[str] | None = None,
    workers: int = SAGEMAKER_DETAIL_WORKERS,
) -> list[SageMakerJob]:
    """Inventaria todos os jobs; detalha métricas apenas dos mais caros."""
    history_window = AnalysisWindow(
        start=window.end - timedelta(days=max(window.days, history_days)),
        end=window.end,
        days=max(window.days, history_days),
    )
    jobs: list[SageMakerJob] = []
    #: Vários transform jobs costumam apontar para o mesmo `Model`; resolvê-lo
    #: uma vez por nome evita repetir `describe_model` a cada execução listada.
    modelos: dict[str, tuple[str, str]] = {}
    for kind, spec in _JOB_SPECS.items():
        listed = safe_pages(
            sagemaker_client,
            spec["list"],
            spec["key"],
            CreationTimeAfter=history_window.start,
            CreationTimeBefore=window.end,
        )
        _gap(listed, spec["list"], gaps)
        summaries = list(listed.items)
        describe_one = partial(
            _describe_job_summary,
            sagemaker_client,
            kind,
            spec,
            window=window,
            history_window=history_window,
            pricing=pricing,
        )
        if workers <= 1 or len(summaries) <= 1:
            described_jobs = [describe_one(summary) for summary in summaries]
        else:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(summaries))
            ) as pool:
                described_jobs = list(pool.map(describe_one, summaries))
        for job, raw, error in described_jobs:
            if job is None:
                continue
            if error:
                _append_gap(gaps, f"{spec['describe']}: {error}")
            if kind == "transform":
                _apply_model_code(
                    sagemaker_client, job, str(raw.get("ModelName") or ""), modelos, gaps
                )
            jobs.append(job)

    selected = jobs if full_metrics else _select_detailed(jobs, detailed_limit)
    _apply_jobs_metrics(cloudwatch_client, selected, history_window)
    _apply_job_owners(sagemaker_client, selected, workers=workers)
    _apply_workload_history(jobs, low_utilization_threshold)
    return jobs


def _describe_job_summary(
    sagemaker_client,
    kind: str,
    spec: dict,
    summary: dict,
    window: AnalysisWindow,
    history_window: AnalysisWindow,
    pricing: Any,
) -> tuple[SageMakerJob | None, dict, str]:
    """Resolve um summary sem tocar listas compartilhadas dentro da thread."""
    name = str(summary.get(spec["name"]) or "")
    if not name:
        return None, {}, ""
    described, error = safe_call(
        sagemaker_client,
        spec["describe"],
        **{spec["arg"]: name},
    )
    raw = {**summary, **described}
    job = _normalize_job(kind, raw, window, history_window)
    _apply_code_location(job, kind, raw)
    _apply_modeled_cost(job, pricing, kind)
    return job, raw, error


def _apply_job_owners(
    sagemaker_client, jobs: list[SageMakerJob], *, workers: int
) -> None:
    """Resolve tags dos jobs selecionados sem serializar uma chamada por ARN."""
    if not jobs:
        return
    get_owner = partial(_owner_for_job, sagemaker_client)
    if workers <= 1 or len(jobs) <= 1:
        owners = [get_owner(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            owners = list(pool.map(get_owner, jobs))
    for job, owner in zip(jobs, owners, strict=True):
        job.owner_tag = owner


def _owner_for_job(sagemaker_client, job: SageMakerJob) -> str | None:
    return _owner(sagemaker_client, {"ResourceArn": job.arn})


def _apply_modeled_cost(job: SageMakerJob, pricing, kind: str) -> None:
    """Custo modelado do job, ou o motivo de não haver — nunca um zero mudo.

    `instance_type` e `instance_count` só existem no `describe`: o summary da
    listagem não os traz. Quando o describe é negado, o job entrava no
    inventário com tipo vazio e contagem zero, e o efeito não parava aí — sem
    base de rateio ele sai do denominador do Cost Explorer, e a cobrança dele é
    redistribuída entre os outros jobs, que passam a parecer mais caros do que
    são. Registrar o motivo é o que torna essa redistribuição visível.

    Job que falhou antes de iniciar é caso distinto e legítimo: ele existiu, a
    configuração é conhecida, e não houve tempo faturável. Zero ali é verdade —
    mas `cost_quality="modeled"` sobre esse zero afirmaria medição, então o campo
    vira `unavailable` com o motivo.
    """
    if not job.instance_type or job.instance_count <= 0:
        job.cost_quality = "unavailable"
        job.cost_unavailable_reason = (
            "configuração de recurso não descrita (describe negado ou ausente)"
        )
        return
    rate = _rate(pricing, job.instance_type, kind)
    if rate is None:
        job.cost_quality = "unavailable"
        job.cost_unavailable_reason = f"sem tarifa {kind} para {job.instance_type}"
        return
    if job.instance_hours <= 0:
        job.cost_quality = "unavailable"
        job.cost_unavailable_reason = (
            "job sem tempo faturável observado (não chegou a iniciar)"
        )
        return
    job.modeled_cost = round(job.instance_hours * rate, 6)
    job.cost_quality = "modeled"


def _normalize_job(
    kind: str,
    raw: dict,
    window: AnalysisWindow,
    history_window: AnalysisWindow,
) -> SageMakerJob:
    spec = _JOB_SPECS[kind]
    started = raw.get("TrainingStartTime") or raw.get("ProcessingStartTime") or raw.get(
        "TransformStartTime"
    )
    ended = raw.get("TrainingEndTime") or raw.get("ProcessingEndTime") or raw.get(
        "TransformEndTime"
    )
    instance_type, count = _job_resources(kind, raw)
    warm = raw.get("WarmPoolStatus") or {}
    experiment = raw.get("ExperimentConfig") or {}
    failure_reason = str(raw.get("FailureReason") or "")
    return SageMakerJob(
        name=str(raw.get(spec["name"]) or ""),
        kind=kind,
        arn=str(raw.get(spec["arn"]) or ""),
        status=str(raw.get(spec["status"]) or ""),
        created_at=_iso(raw.get("CreationTime")),
        started_at=_iso(started),
        ended_at=_iso(ended),
        instance_type=instance_type,
        instance_count=count,
        duration_seconds=_seconds_between(started, ended),
        billable_seconds=_optional_float(raw.get("BillableTimeInSeconds")),
        training_seconds=_optional_float(raw.get("TrainingTimeInSeconds")),
        use_spot=bool(raw.get("EnableManagedSpotTraining", False)),
        checkpoint_configured=bool(raw.get("CheckpointConfig")),
        keep_alive_seconds=int(
            (raw.get("ResourceConfig") or {}).get("KeepAlivePeriodInSeconds") or 0
        ),
        warm_pool_status=str(warm.get("Status") or ""),
        warm_pool_billable_seconds=float(
            warm.get("ResourceRetainedBillableTimeInSeconds") or 0.0
        ),
        warm_pool_reused=bool(warm.get("ReusedByJob"))
        or str(warm.get("Status") or "").lower() == "reused",
        failure_category=_failure_category(failure_reason),
        pipeline_name=str(
            experiment.get("ExperimentName")
            or experiment.get("TrialName")
            or ""
        ),
        workload_fingerprint=_workload_fingerprint(kind, raw),
        history_coverage_days=history_window.days,
        in_financial_window=window.contains(
            started if isinstance(started, datetime) else raw.get("CreationTime")
        ),
        coverage_days=window.days,
    )


def _workload_fingerprint(kind: str, raw: dict) -> str:
    experiment = raw.get("ExperimentConfig") or {}
    pipeline = str(
        experiment.get("ExperimentName")
        or experiment.get("TrialName")
        or ""
    )
    if kind == "training":
        spec = raw.get("AlgorithmSpecification") or {}
        identity = spec.get("TrainingImage") or spec.get("AlgorithmName")
    elif kind == "processing":
        identity = (raw.get("AppSpecification") or {}).get("ImageUri")
    else:
        identity = raw.get("ModelName")
    material = "|".join(
        value
        for value in (kind, pipeline, str(identity or ""))
        if value
    )
    if material == kind:
        return ""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _apply_workload_history(
    jobs: list[SageMakerJob],
    low_utilization_threshold: float,
) -> None:
    groups: dict[str, list[SageMakerJob]] = defaultdict(list)
    for job in jobs:
        if job.workload_fingerprint:
            groups[job.workload_fingerprint].append(job)
    for group in groups.values():
        low = [
            job
            for job in group
            if job.detailed_metrics
            and _metrics_low(job, low_utilization_threshold)
        ]
        for job in group:
            job.workload_runs = len(group)
            job.low_utilization_runs = len(low)


def _metrics_low(job: SageMakerJob, threshold: float) -> bool:
    values = [
        value
        for value in (job.cpu_p95, job.gpu_p95, job.memory_p95)
        if value is not None
    ]
    return bool(values) and max(values) / 100.0 < threshold


def _apply_code_location(job: SageMakerJob, kind: str, raw: dict) -> None:
    """Onde está o código que o job roda — os ponteiros já vêm no `describe`.

    O SDK do SageMaker empacota o script mode em hiperparâmetros: o diretório
    vai para `sagemaker_submit_directory` e o arquivo de entrada para
    `sagemaker_program`, ambos com aspas JSON embutidas no valor. Processing
    usa outro caminho — o script chega como um `ProcessingInput` chamado
    `code`. Nenhum dos dois custa chamada nova: o payload já estava em mãos e
    era descartado.

    Algoritmo gerenciado da AWS não tem script do cliente, e isso é resposta,
    não falha. Sem registrar o motivo, um XGBoost gerenciado e um job cujo
    `describe` foi negado ficam idênticos: dois jobs sem análise de código.
    """
    if kind == "training":
        hiper = raw.get("HyperParameters") or {}
        diretorio = _sem_aspas(hiper.get("sagemaker_submit_directory"))
        programa = _sem_aspas(hiper.get("sagemaker_program"))
        if diretorio and programa:
            job.code_location = diretorio
            job.code_entry_point = programa
            job.code_kind = (
                "sourcedir_tar" if diretorio.endswith(".tar.gz") else "s3_object"
            )
            return
        algoritmo = (raw.get("AlgorithmSpecification") or {}).get("AlgorithmName")
        imagem = (raw.get("AlgorithmSpecification") or {}).get("TrainingImage")
        if algoritmo or imagem:
            job.code_kind = "builtin_algorithm"
            job.code_unavailable_reason = (
                "algoritmo gerenciado ou imagem própria sem script mode: "
                "não há código do cliente para ler"
            )
            return
        job.code_unavailable_reason = "describe negado ou sem AlgorithmSpecification"
        return

    if kind == "processing":
        for entrada in raw.get("ProcessingInputs") or ():
            if not isinstance(entrada, dict):
                continue
            if str(entrada.get("InputName") or "").lower() != "code":
                continue
            uri = str(((entrada.get("S3Input") or {}).get("S3Uri")) or "")
            if uri:
                job.code_location = uri
                job.code_kind = (
                    "sourcedir_tar" if uri.endswith(".tar.gz") else "s3_object"
                )
                return
        entrypoint = (raw.get("AppSpecification") or {}).get("ContainerEntrypoint")
        if entrypoint:
            job.code_kind = "container_entrypoint"
            job.code_unavailable_reason = (
                "script embutido na imagem do contêiner: fora do alcance de uma "
                "coleta read-only do S3"
            )
            return
        job.code_unavailable_reason = "describe negado ou sem ProcessingInputs"
        return

    # Transform não carrega código próprio: ele roda o modelo. O ponteiro está
    # no `Model`, e resolvê-lo é uma chamada a mais — feita em `collect_jobs`,
    # onde o cliente existe e o resultado pode ser reaproveitado entre jobs.
    job.code_unavailable_reason = "código do transform vem do Model associado"


def _apply_model_code(
    sagemaker_client,
    job: SageMakerJob,
    model_name: str,
    cache: dict[str, tuple[str, str]],
    gaps: list[str] | None,
) -> None:
    """O código de um transform vive no `Model` que ele executa.

    O SDK guarda o mesmo par diretório/programa do script mode nas variáveis de
    ambiente do contêiner primário. Modelo sem elas roda inferência embutida na
    imagem, e aí não há o que ler.
    """
    if not model_name:
        return
    if model_name not in cache:
        described, error = safe_call(
            sagemaker_client, "describe_model", ModelName=model_name
        )
        if error:
            _append_gap(gaps, f"describe_model: {error}")
            job.code_unavailable_reason = f"describe_model negado: {error}"
            return
        ambiente = (described.get("PrimaryContainer") or {}).get("Environment") or {}
        cache[model_name] = (
            _sem_aspas(ambiente.get("SAGEMAKER_SUBMIT_DIRECTORY")),
            _sem_aspas(ambiente.get("SAGEMAKER_PROGRAM")),
        )
    diretorio, programa = cache[model_name]
    if not (diretorio and programa):
        job.code_unavailable_reason = (
            f"modelo {model_name} não declara script mode: inferência embutida na imagem"
        )
        return
    job.code_location = diretorio
    job.code_entry_point = programa
    job.code_kind = "sourcedir_tar" if diretorio.endswith(".tar.gz") else "s3_object"
    job.code_unavailable_reason = ""


def _sem_aspas(valor: Any) -> str:
    """Hiperparâmetro do SageMaker chega como `'"train.py"'`."""
    return str(valor or "").strip().strip('"')


def _job_resources(kind: str, raw: dict) -> tuple[str, int]:
    if kind == "training":
        config = raw.get("ResourceConfig") or {}
    elif kind == "processing":
        config = (raw.get("ProcessingResources") or {}).get("ClusterConfig") or {}
    else:
        config = raw.get("TransformResources") or {}
    return (
        str(config.get("InstanceType") or ""),
        int(config.get("InstanceCount") or 0),
    )


def _select_detailed(jobs: list[SageMakerJob], limit: int) -> list[SageMakerJob]:
    if limit <= 0 or not jobs:
        return []
    by_kind: dict[str, list[SageMakerJob]] = defaultdict(list)
    for job in jobs:
        by_kind[job.kind].append(job)
    for group in by_kind.values():
        group.sort(key=_potential_cost, reverse=True)

    selected: list[SageMakerJob] = []
    seen: set[tuple[str, str]] = set()
    floor = min(10, max(1, limit // max(1, len(by_kind))))
    for kind in sorted(by_kind):
        for job in by_kind[kind][:floor]:
            selected.append(job)
            seen.add((job.kind, job.name))
    for job in sorted(jobs, key=_potential_cost, reverse=True):
        if len(selected) >= limit:
            break
        key = (job.kind, job.name)
        if key not in seen:
            selected.append(job)
            seen.add(key)
    return selected[:limit]


def _potential_cost(job: SageMakerJob) -> float:
    if job.modeled_cost is not None:
        return job.modeled_cost
    gpu_weight = 10.0 if _gpu(job.instance_type) else 1.0
    return job.instance_hours * gpu_weight


def _apply_job_metrics(
    cloudwatch_client, job: SageMakerJob, window: AnalysisWindow
) -> None:
    """Compatibilidade para chamadores pontuais; o coletor usa o lote abaixo."""
    _apply_jobs_metrics(cloudwatch_client, [job], window)


_JOB_METRICS = (
    ("CPUUtilization", "cpu_p95"),
    ("GPUUtilization", "gpu_p95"),
    ("MemoryUtilization", "memory_p95"),
    ("DiskUtilization", "disk_p95"),
)
_MAX_CLOUDWATCH_QUERIES = 500


def _apply_jobs_metrics(
    cloudwatch_client, jobs: list[SageMakerJob], window: AnalysisWindow
) -> None:
    """Agrupa métricas de vários jobs em chamadas de até 500 consultas.

    O caminho anterior fazia um `GetMetricData` por job. Quatro métricas para
    os 100 jobs detalhados cabem numa única requisição inicial; `NextToken`
    continua sendo seguido quando a quantidade de pontos exige paginação.
    """
    if cloudwatch_client is None:
        return
    pending: list[tuple[dict, SageMakerJob, str]] = []
    for job_index, job in enumerate(jobs):
        namespace = _JOB_SPECS[job.kind]["namespace"]
        for metric_index, (metric, field) in enumerate(_JOB_METRICS):
            query_id = f"j{job_index}m{metric_index}"
            pending.append((
            {
                "Id": query_id,
                "Expression": (
                    f"SEARCH('{{{namespace},Host}} MetricName=\"{metric}\" "
                    f"\"{job.name}\"', 'Average', 3600)"
                ),
                "Label": query_id,
                "ReturnData": True,
            },
            job,
            field,
        ))

    for offset in range(0, len(pending), _MAX_CLOUDWATCH_QUERIES):
        block = pending[offset : offset + _MAX_CLOUDWATCH_QUERIES]
        payload = [query for query, _job, _field in block]
        targets: dict[str, tuple[SageMakerJob, str, list[float]]] = {
            str(query["Id"]): (job, field, [])
            for query, job, field in block
        }
        token = None
        try:
            while True:
                kwargs = {
                    "MetricDataQueries": payload,
                    "StartTime": window.start,
                    "EndTime": window.end,
                    "ScanBy": "TimestampAscending",
                }
                if token:
                    kwargs["NextToken"] = token
                response = cloudwatch_client.get_metric_data(**kwargs)
                for result in response.get("MetricDataResults", []) or []:
                    target = targets.get(str(result.get("Id") or ""))
                    if target is None:
                        continue
                    target[2].extend(
                        float(value) for value in result.get("Values", []) or []
                    )
                token = response.get("NextToken")
                if not token:
                    break
        except Exception:
            # Mantém o isolamento anterior: uma falha de lote não inventa zero
            # e não marca os jobs como detalhados.
            continue
        for job, field, values in targets.values():
            setattr(job, field, _p95(values))
            job.detailed_metrics = True


def collect_feature_groups(
    sagemaker_client,
    cloudwatch_client=None,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerFeatureGroup]:
    listed = safe_pages(sagemaker_client, "list_feature_groups", "FeatureGroupSummaries")
    _gap(listed, "list_feature_groups", gaps)
    groups: list[SageMakerFeatureGroup] = []
    for summary in listed.items:
        name = str(summary.get("FeatureGroupName") or "")
        if not name:
            continue
        described, error = safe_call(
            sagemaker_client, "describe_feature_group", FeatureGroupName=name
        )
        if error:
            _append_gap(gaps, f"describe_feature_group: {error}")
        raw = {**summary, **described}
        online = raw.get("OnlineStoreConfig") or {}
        offline = raw.get("OfflineStoreConfig") or {}
        throughput = raw.get("ThroughputConfig") or online.get("ThroughputConfig") or {}
        ttl = online.get("TtlDuration") or {}
        group = SageMakerFeatureGroup(
            name=name,
            arn=str(raw.get("FeatureGroupArn") or ""),
            status=str(raw.get("FeatureGroupStatus") or ""),
            online_store=bool(online.get("EnableOnlineStore", bool(online))),
            offline_store=bool(offline),
            storage_type=str(online.get("StorageType") or ""),
            throughput_mode=str(throughput.get("ThroughputMode") or ""),
            provisioned_read_capacity=int(
                throughput.get("ProvisionedReadCapacityUnits") or 0
            ),
            provisioned_write_capacity=int(
                throughput.get("ProvisionedWriteCapacityUnits") or 0
            ),
            ttl_seconds=_ttl_seconds(ttl),
            coverage_days=window.days,
            owner_tag=_owner(sagemaker_client, raw),
        )
        _apply_feature_metrics(cloudwatch_client, group, window)
        groups.append(group)
    return groups


def _apply_feature_metrics(
    cloudwatch_client, group: SageMakerFeatureGroup, window: AnalysisWindow
) -> None:
    if cloudwatch_client is None:
        return
    metrics = {
        "ConsumedReadCapacityUnits": "max_consumed_read_capacity",
        "ConsumedWriteCapacityUnits": "max_consumed_write_capacity",
        "ConsumedReadRequestsUnits": "consumed_read_request_units",
        "ConsumedWriteRequestsUnits": "consumed_write_request_units",
        "ThrottledRequests": "throttled_requests",
        "InternalFailure": "server_errors",
    }
    for metric, field_name in metrics.items():
        result = _search_metric(
            cloudwatch_client,
            namespace="AWS/SageMaker",
            metric=metric,
            token=group.name,
            window=window,
            statistic="Maximum" if metric.startswith("Consumed") else "Sum",
        )
        if result is None:
            continue
        setattr(
            group,
            field_name,
            max(result) if metric.endswith("CapacityUnits") else sum(result),
        )


def collect_pipelines(
    sagemaker_client,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerPipeline]:
    listed = safe_pages(
        sagemaker_client,
        "list_pipelines",
        "PipelineSummaries",
    )
    _gap(listed, "list_pipelines", gaps)
    pipelines: list[SageMakerPipeline] = []
    for summary in listed.items:
        name = str(summary.get("PipelineName") or "")
        if not name:
            continue
        described, _ = safe_call(
            sagemaker_client, "describe_pipeline", PipelineName=name
        )
        executions = safe_pages(
            sagemaker_client,
            "list_pipeline_executions",
            "PipelineExecutionSummaries",
            PipelineName=name,
            CreatedAfter=window.start,
            CreatedBefore=window.end,
        )
        _gap(executions, "list_pipeline_executions", gaps)
        durations: list[float] = []
        counts: dict[str, int] = defaultdict(int)
        job_names: set[str] = set()
        for execution in executions.items:
            status = str(execution.get("PipelineExecutionStatus") or "")
            counts[status] += 1
            durations.append(
                _seconds_between(
                    execution.get("StartTime"), execution.get("LastModifiedTime")
                )
            )
            arn = str(execution.get("PipelineExecutionArn") or "")
            if arn:
                steps = safe_pages(
                    sagemaker_client,
                    "list_pipeline_execution_steps",
                    "PipelineExecutionSteps",
                    PipelineExecutionArn=arn,
                )
                _gap(steps, "list_pipeline_execution_steps", gaps)
                for step in steps.items:
                    metadata = step.get("Metadata") or {}
                    for value in metadata.values():
                        if isinstance(value, dict):
                            name_value = next(
                                (
                                    val
                                    for key, val in value.items()
                                    if key.endswith("JobName")
                                ),
                                "",
                            )
                            if name_value:
                                job_names.add(str(name_value))
        pipelines.append(
            SageMakerPipeline(
                name=name,
                arn=str(
                    described.get("PipelineArn")
                    or summary.get("PipelineArn")
                    or ""
                ),
                status=str(described.get("PipelineStatus") or ""),
                executions=len(executions.items),
                succeeded=counts.get("Succeeded", 0),
                failed=counts.get("Failed", 0),
                stopped=counts.get("Stopped", 0),
                avg_duration_seconds=(
                    sum(durations) / len(durations) if durations else None
                ),
                job_names=sorted(job_names),
                coverage_days=window.days,
                owner_tag=_owner(sagemaker_client, described, summary),
            )
        )
    return pipelines


def collect_monitoring_schedules(
    sagemaker_client,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerMonitoringSchedule]:
    """Inventaria somente schedules de Model Monitor que já existem."""
    listed = safe_pages(
        sagemaker_client,
        "list_monitoring_schedules",
        "MonitoringScheduleSummaries",
    )
    _gap(listed, "list_monitoring_schedules", gaps)
    schedules: list[SageMakerMonitoringSchedule] = []
    for summary in listed.items:
        name = str(summary.get("MonitoringScheduleName") or "")
        if not name:
            continue
        described, error = safe_call(
            sagemaker_client,
            "describe_monitoring_schedule",
            MonitoringScheduleName=name,
        )
        if error:
            _append_gap(gaps, f"describe_monitoring_schedule: {error}")
        raw = {**summary, **described}
        latest = raw.get("LatestMonitoringExecutionSummary") or {}
        config = raw.get("MonitoringScheduleConfig") or {}
        job_definition = (
            config.get("MonitoringJobDefinition")
            or config.get("MonitoringJobDefinitionName")
            or {}
        )
        endpoint = ""
        if isinstance(job_definition, dict):
            inputs = job_definition.get("MonitoringInputs") or []
            if inputs and isinstance(inputs[0], dict):
                endpoint = str(
                    inputs[0].get("EndpointInput", {}).get("EndpointName", "")
                )
        schedules.append(
            SageMakerMonitoringSchedule(
                name=name,
                arn=str(raw.get("MonitoringScheduleArn") or ""),
                status=str(raw.get("MonitoringScheduleStatus") or ""),
                monitoring_type=str(raw.get("MonitoringType") or ""),
                endpoint_name=endpoint,
                last_execution_status=str(
                    latest.get("MonitoringExecutionStatus") or ""
                ),
                last_execution_time=_iso(
                    latest.get("ScheduledTime") or latest.get("CreationTime")
                ),
                failure_reason=str(latest.get("FailureReason") or ""),
                coverage_days=window.days,
                owner_tag=_owner(sagemaker_client, raw),
            )
        )
    return schedules


def collect_inference_recommendations(
    sagemaker_client,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerInferenceRecommendation]:
    listed = safe_pages(
        sagemaker_client,
        "list_inference_recommendations_jobs",
        "InferenceRecommendationsJobs",
        CreationTimeAfter=window.start,
        CreationTimeBefore=window.end,
    )
    _gap(listed, "list_inference_recommendations_jobs", gaps)
    out: list[SageMakerInferenceRecommendation] = []
    for summary in listed.items:
        name = str(summary.get("JobName") or "")
        if not name:
            continue
        described, error = safe_call(
            sagemaker_client,
            "describe_inference_recommendations_job",
            JobName=name,
        )
        if error:
            _append_gap(gaps, f"describe_inference_recommendations_job: {error}")
        recommendations = described.get("InferenceRecommendations") or []
        if not recommendations:
            out.append(
                SageMakerInferenceRecommendation(
                    job_name=name,
                    status=str(described.get("Status") or summary.get("Status") or ""),
                    model_name=str((described.get("InputConfig") or {}).get("ModelName") or ""),
                    created_at=_iso(
                        described.get("CreationTime") or summary.get("CreationTime")
                    ),
                    coverage_days=window.days,
                )
            )
            continue
        for recommendation in recommendations:
            endpoint = recommendation.get("EndpointConfiguration") or {}
            metrics = recommendation.get("Metrics") or {}
            out.append(
                SageMakerInferenceRecommendation(
                    job_name=name,
                    status=str(described.get("Status") or summary.get("Status") or ""),
                    model_name=str((described.get("InputConfig") or {}).get("ModelName") or ""),
                    endpoint_name=str(endpoint.get("EndpointName") or ""),
                    recommended_instance_type=str(endpoint.get("InstanceType") or ""),
                    initial_instance_count=int(endpoint.get("InitialInstanceCount") or 0),
                    max_invocations=_optional_float(metrics.get("MaxInvocations")),
                    model_latency_ms=_optional_float(metrics.get("ModelLatency")),
                    cost_per_hour=_optional_float(metrics.get("CostPerHour")),
                    created_at=_iso(
                        described.get("CreationTime") or summary.get("CreationTime")
                    ),
                    coverage_days=window.days,
                )
            )
    return out


def _search_metric(
    cloudwatch_client,
    *,
    namespace: str,
    metric: str,
    token: str,
    window: AnalysisWindow,
    statistic: str,
) -> list[float] | None:
    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "m0",
                    "Expression": (
                        f"SEARCH('{{{namespace}}} MetricName=\"{metric}\" "
                        f"\"{token}\"', '{statistic}', 3600)"
                    ),
                    "Label": metric,
                    "ReturnData": True,
                }
            ],
            StartTime=window.start,
            EndTime=window.end,
            ScanBy="TimestampAscending",
        )
    except Exception:
        return None
    return [
        float(value)
        for result in response.get("MetricDataResults", []) or []
        for value in result.get("Values", []) or []
    ]


def _owner(client, *resources: dict) -> str | None:
    for resource in resources:
        dono = owner_from_tags(resource.get("Tags"))
        if dono:
            return dono
    arn = next(
        (
            str(resource.get(key) or "")
            for resource in resources
            for key in (
                "ResourceArn",
                "NotebookInstanceArn",
                "FeatureGroupArn",
                "PipelineArn",
            )
            if resource.get(key)
        ),
        "",
    )
    if not arn:
        return None
    response, _ = safe_call(client, "list_tags", ResourceArn=arn)
    return owner_from_tags(response.get("Tags"))


def _rate(pricing: Any, instance_type: str, component: str) -> float | None:
    if pricing is None or not instance_type:
        return None
    try:
        return pricing.sagemaker_hourly(instance_type, component)
    except TypeError:
        return pricing.sagemaker_hourly(instance_type)


def _gpu(instance_type: str) -> bool:
    return any(
        f".{family}" in instance_type
        for family in ("g3", "g4", "g5", "g6", "g7", "p2", "p3", "p4", "p5", "p6")
    )


def _failure_category(reason: str) -> str:
    lowered = reason.lower()
    if not lowered:
        return ""
    for marker, category in (
        ("accessdenied", "permission_denied"),
        ("resource limit", "resource_limit"),
        ("capacity", "capacity"),
        ("timeout", "timeout"),
        ("algorithm", "algorithm_error"),
        ("client", "client_error"),
        ("internal", "service_error"),
    ):
        if marker in lowered:
            return category
    return "other"


def _ttl_seconds(raw: dict) -> int | None:
    value = raw.get("Value")
    if value is None:
        return None
    multiplier = {
        "Seconds": 1,
        "Minutes": 60,
        "Hours": 3600,
        "Days": 86400,
        "Weeks": 604800,
    }.get(str(raw.get("Unit") or ""), 1)
    return int(value) * multiplier


def _seconds_between(start: object, end: object) -> float:
    if isinstance(start, datetime) and isinstance(end, datetime):
        return max(0.0, (end - start).total_seconds())
    return 0.0


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 4)
    return round(quantiles(values, n=100, method="inclusive")[94], 4)


def _gap(result, operation: str, gaps: list[str] | None) -> None:
    if not result.complete:
        _append_gap(gaps, f"{operation}: {result.error_category or 'incompleto'}")


def _append_gap(gaps: list[str] | None, value: str) -> None:
    if gaps is not None and value not in gaps:
        gaps.append(value)
