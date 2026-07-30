"""Coletor CloudWatch: enriquece os Glue Jobs com CPU e observabilidade.

O Glue publica métricas no namespace "Glue". `glue.ALL.system.cpuSystemLoad`
(carga média dos executores, 0–1) preenche `avg_cpu_load` e destrava as regras
de capacidade; as métricas de observabilidade preenchem memória, disco, skew e
executores.

São onze métricas por job. Uma chamada `GetMetricStatistics` por métrica dava
onze idas à AWS por job — 3.300 numa conta com 300 jobs, em série, cada uma
pagando a latência inteira. Em lote as mesmas 3.300 consultas cabem em sete
chamadas.

O mecanismo do lote mora em `collectors/metrics.py`: ele nasceu aqui, mas o
namespace era constante do módulo e as dimensões eram fixas em `JobName`, então
outros seis coletores com o mesmo problema não conseguiam usá-lo. O que fica
aqui é o que é do Glue — quais métricas importam, quais dimensões cada uma pede
e como o valor da janela é reduzido.

O que não muda: `Maximum` continua `Maximum` e `Sum` continua `Sum`. Não
suavizar pressão de memória, disco e skew é decisão de comportamento — o gate de
capacidade precisa do pior pico da janela, não da média dele. E ausência de
métrica continua `None`, nunca zero: zero significaria "medido e vazio".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from statistics import mean

from julius.collection.collectors import metrics as metric_batch
from julius.collection.collectors.metrics import (
    DAILY_PERIOD_SECONDS,
    MAX_QUERIES_PER_CALL,
    MetricQuery,
)
from julius.collection.models import GlueJob
from julius.collection.window import AnalysisWindow

_NAMESPACE = "Glue"
_PERIOD_SECONDS = DAILY_PERIOD_SECONDS

__all__ = [
    "MAX_QUERIES_PER_CALL",
    "enrich_glue_cpu",
    "enrich_glue_observability",
]

_CPU_METRIC = "glue.ALL.system.cpuSystemLoad"


@dataclass(frozen=True)
class _Metric:
    """Uma métrica do Glue e como ela vira um número da janela.

    O `ObservabilityGroup` era deduzido do nome da métrica por uma cadeia de
    `startswith`/`in`. Passou a ser declarado aqui: a dimensão é um fato da
    métrica, não algo a inferir do texto do nome dela.
    """

    name: str
    stat: str
    reduce: Callable[[Sequence[float]], float]
    extra_dimensions: tuple[tuple[str, str], ...] = ()
    count_type: bool = False


def _mean(values: Sequence[float]) -> float:
    return mean(values)


def _max(values: Sequence[float]) -> float:
    return max(values)


def _sum(values: Sequence[float]) -> float:
    return sum(values)


_RESOURCE = (("ObservabilityGroup", "resource_utilization"),)
_PERFORMANCE = (("ObservabilityGroup", "job_performance"),)

_CPU: dict[str, _Metric] = {
    # A única sem `JobRunId`: a carga de CPU é publicada por job, não por run.
    "avg_cpu_load": _Metric(_CPU_METRIC, "Average", _mean),
}

_OBSERVABILITY: dict[str, _Metric] = {
    "avg_worker_utilization": _Metric(
        "glue.driver.workerUtilization", "Average", _mean, _RESOURCE
    ),
    "max_memory_used_pct": _Metric(
        "glue.ALL.memory.total.used.percentage", "Maximum", _max, _RESOURCE
    ),
    "max_disk_used_pct": _Metric(
        "glue.ALL.disk.used.percentage", "Maximum", _max, _RESOURCE
    ),
    "max_task_skew": _Metric(
        "glue.driver.skewness.job", "Maximum", _max, _PERFORMANCE
    ),
    "avg_all_executors": _Metric(
        "glue.driver.ExecutorAllocationManager.executors.numberAllExecutors",
        "Average",
        _mean,
    ),
    "avg_max_needed_executors": _Metric(
        "glue.driver.ExecutorAllocationManager.executors.numberMaxNeededExecutors",
        "Average",
        _mean,
    ),
    # Somas da janela: métricas de contagem, agregadas em JobRunId=ALL.
    "bytes_read_window": _Metric(
        "glue.driver.aggregate.bytesRead", "Sum", _sum, count_type=True
    ),
    "bytes_written_window": _Metric(
        "glue.driver.throughput.bytesWritten",
        "Sum",
        _sum,
        (("ObservabilityGroup", "throughput"), ("Sink", "ALL")),
        count_type=True,
    ),
    "files_written_window": _Metric(
        "glue.driver.throughput.filesWritten",
        "Sum",
        _sum,
        (("ObservabilityGroup", "throughput"), ("Sink", "ALL")),
        count_type=True,
    ),
    "streaming_records_window": _Metric(
        "glue.driver.streaming.numRecords", "Sum", _sum, count_type=True
    ),
}


@dataclass
class _Request:
    """Uma consulta pendente e onde o resultado dela aterrissa."""

    job: GlueJob
    field_name: str
    metric: _Metric
    values: list[float] = field(default_factory=list)


def enrich_glue_cpu(cw_client, jobs: list[GlueJob], *, window: AnalysisWindow) -> None:
    """Preenche `avg_cpu_load` (0–1) de cada job in place."""
    _enrich(cw_client, jobs, _CPU, window)


def enrich_glue_observability(
    cw_client, jobs: list[GlueJob], *, window: AnalysisWindow
) -> None:
    """Coleta os sinais que tornam recomendações de capacidade acionáveis."""
    _enrich(cw_client, jobs, _OBSERVABILITY, window)


def _enrich(
    cw_client,
    jobs: list[GlueJob],
    metrics: dict[str, _Metric],
    window: AnalysisWindow,
) -> None:
    requests = [
        _Request(job=job, field_name=name, metric=metric)
        for job in jobs
        for name, metric in metrics.items()
    ]
    queries = [
        MetricQuery(
            namespace=_NAMESPACE,
            metric_name=request.metric.name,
            stat=request.metric.stat,
            dimensions=_dimensions(request),
            period=_PERIOD_SECONDS,
        )
        for request in requests
    ]
    metric_batch.collect(cw_client, queries, start=window.start, end=window.end)

    for request, query in zip(requests, queries, strict=True):
        if query.values:
            setattr(
                request.job,
                request.field_name,
                round(request.metric.reduce(query.values), 3),
            )


def _dimensions(request: _Request) -> tuple[tuple[str, str], ...]:
    dimensions = [("JobName", request.job.name)]
    if request.metric.name != _CPU_METRIC:
        dimensions.append(("JobRunId", "ALL"))
    dimensions.append(("Type", "count" if request.metric.count_type else "gauge"))
    dimensions.extend(request.metric.extra_dimensions)
    return tuple(dimensions)
