"""Coletor CloudWatch: enriquece os Glue Jobs com CPU e observabilidade.

O Glue publica métricas no namespace "Glue". `glue.ALL.system.cpuSystemLoad`
(carga média dos executores, 0–1) preenche `avg_cpu_load` e destrava as regras
de capacidade; as métricas de observabilidade preenchem memória, disco, skew e
executores.

São onze métricas por job. Uma chamada `GetMetricStatistics` por métrica dava
onze idas à AWS por job — 3.300 numa conta com 300 jobs, em série, cada uma
pagando a latência inteira. `GetMetricData` aceita **500 consultas por chamada**
(e 100.800 pontos; com período diário em 30 dias são 30 pontos por consulta, de
modo que o limite que vale é o de 500), então as mesmas 3.300 consultas cabem em
sete chamadas.

O que não muda: `Maximum` continua `Maximum` e `Sum` continua `Sum`. Não
suavizar pressão de memória, disco e skew é decisão de comportamento — o gate de
capacidade precisa do pior pico da janela, não da média dele. E ausência de
métrica continua `None`, nunca zero: zero significaria "medido e vazio".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean

from julius.collection.models import GlueJob
from julius.collection.window import AnalysisWindow

_NAMESPACE = "Glue"
_PERIOD_SECONDS = 86400  # um ponto por dia
#: Teto da API para consultas numa única chamada `GetMetricData`.
MAX_QUERIES_PER_CALL = 500

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
    for block in _blocks(requests, MAX_QUERIES_PER_CALL):
        # O isolamento de falha muda de grão junto com o lote: antes uma métrica
        # ruim afetava uma métrica, agora uma chamada ruim afeta o bloco. O que
        # não muda é o efeito — os campos do bloco ficam `None`, exatamente como
        # ficariam se a métrica não existisse.
        try:
            _fetch(cw_client, block, window.start, window.end)
        except Exception:
            continue
        for request in block:
            if request.values:
                setattr(
                    request.job,
                    request.field_name,
                    round(request.metric.reduce(request.values), 3),
                )


def _blocks(
    requests: list[_Request], size: int
) -> Iterator[list[_Request]]:
    for start in range(0, len(requests), size):
        yield requests[start : start + size]


def _fetch(
    cw_client, block: list[_Request], start: datetime, end: datetime
) -> None:
    """Executa um bloco e acumula os valores por consulta, página a página."""
    by_id = {f"m{index}": request for index, request in enumerate(block)}
    queries = [
        {
            "Id": query_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": _NAMESPACE,
                    "MetricName": request.metric.name,
                    "Dimensions": _dimensions(request),
                },
                "Period": _PERIOD_SECONDS,
                "Stat": request.metric.stat,
            },
            "ReturnData": True,
        }
        for query_id, request in by_id.items()
    ]

    token = None
    while True:
        kwargs = {
            "MetricDataQueries": queries,
            "StartTime": start,
            "EndTime": end,
        }
        if token:
            kwargs["NextToken"] = token
        response = cw_client.get_metric_data(**kwargs)
        for result in response.get("MetricDataResults", []):
            request = by_id.get(str(result.get("Id")))
            if request is None:
                continue
            request.values.extend(float(value) for value in result.get("Values", []))
        token = response.get("NextToken")
        if not token:
            return


def _dimensions(request: _Request) -> list[dict[str, str]]:
    dimensions = [{"Name": "JobName", "Value": request.job.name}]
    if request.metric.name != _CPU_METRIC:
        dimensions.append({"Name": "JobRunId", "Value": "ALL"})
    dimensions.append(
        {"Name": "Type", "Value": "count" if request.metric.count_type else "gauge"}
    )
    dimensions.extend(
        {"Name": name, "Value": value} for name, value in request.metric.extra_dimensions
    )
    return dimensions
