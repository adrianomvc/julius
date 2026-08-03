"""Contagem e ordem quando a coleta usa mais de uma thread.

A coleta paraleliza por alvo — prefixos S3 e histórico de execuções do Glue — e
duas coisas não podem depender de quem chega primeiro: a contagem do custo do
próprio scan, que alimenta o `--max-scan-cost`, e a ordem do dataset, que é o
que permite comparar dois scans.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from time import sleep

from julius.collection.collectors.athena import executions as athena_executions
from julius.collection.collectors.glue import jobs as glue_jobs
from julius.collection.collectors.sagemaker_extended import (
    _apply_job_owners,
    _apply_jobs_metrics,
)
from julius.collection.collectors.sagemaker_extended import (
    collect_jobs as collect_sagemaker_jobs,
)
from julius.collection.collectors.stepfunctions import collect_state_machines
from julius.collection.models import SageMakerJob
from julius.collection.telemetry import InstrumentedClient, RunTelemetry
from julius.collection.window import AnalysisWindow


def test_concurrent_first_touch_does_not_lose_counts():
    """A perda vinha da estreia simultânea, não do incremento.

    O `stat()` antigo fazia `if key not in api_calls: api_calls[key] = ...` —
    duas operações. Duas threads estreando a mesma operação criavam dois
    `ApiCallStat`, e a segunda atribuição descartava o que a primeira já tinha
    contado. Medido no padrão antigo com este mesmo arranjo: 211 chamadas
    perdidas em 20 rodadas (CPython 3.14, GIL ligado).

    A barreira é o que dá dentes ao teste: sem ela as threads entram
    escalonadas e a estreia deixa de ser concorrente.
    """
    threads, operacoes = 16, 40
    telemetry = RunTelemetry()
    largada = Barrier(threads)

    def contar(_indice: int) -> None:
        largada.wait()
        for numero in range(operacoes):
            telemetry.stat("glue", f"op_{numero}").add(calls=1)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(contar, range(threads)))

    assert len(telemetry.api_calls) == operacoes
    total = sum(stat.calls for stat in telemetry.api_calls.values())
    assert total == threads * operacoes


def test_repeated_increments_on_one_stat_stay_exact():
    """Contagem no mesmo stat, o caso da listagem de um único bucket grande."""
    telemetry = RunTelemetry()
    vezes, threads = 500, 8

    def contar() -> None:
        for _ in range(vezes):
            telemetry.stat("s3", "list_objects_v2").add(calls=1, pages=1)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for _ in range(threads):
            pool.submit(contar)

    stat = telemetry.stat("s3", "list_objects_v2")
    assert stat.calls == vezes * threads
    assert stat.pages == vezes * threads


def test_the_lock_stays_out_of_the_dataset():
    """O manifesto do scan é serializado com `asdict`; lock não é dado."""
    telemetry = RunTelemetry()
    telemetry.stat("s3", "list_objects_v2").add(calls=1)

    payload = asdict(telemetry)

    # Serializável sem tratamento especial: é assim que `dump.py` o escreve.
    json.dumps(payload)
    assert "_lock" not in payload
    assert "_lock" not in payload["api_calls"]["s3:list_objects_v2"]


class _GluePaginator:
    def __init__(self, glue: _Glue, pages: list[dict] | None) -> None:
        self._glue = glue
        self._pages = pages

    def paginate(self, **kwargs):
        nome = kwargs.get("JobName")
        if nome is None:
            return iter(self._pages or [])
        if nome in self._glue.negados:
            raise PermissionError("AccessDeniedException")
        # Latência simulada: sem ela as threads terminam na ordem de submissão e
        # o teste deixaria de exercitar o entrelaçamento.
        sleep(0.002)
        return iter([{"JobRuns": self._glue.runs[nome]}])


class _Glue:
    """Glue mínimo: lista N jobs e devolve o histórico de cada um.

    A duração é distinta por job de propósito: se o paralelismo trocar o
    histórico de um job pelo de outro, a contagem continua certa e o número
    muda — defeito que uma asserção de tamanho não pegaria.
    """

    def __init__(
        self, quantidade: int, *, agora: datetime, negados: set[str] | None = None
    ) -> None:
        self.negados = negados or set()
        self.jobs = [
            {
                "Name": f"job-{indice:03d}",
                "Command": {"Name": "glueetl"},
                "WorkerType": "G.1X",
                "NumberOfWorkers": 2,
                "Timeout": 2880,
            }
            for indice in range(quantidade)
        ]
        inicio = agora - timedelta(days=1)
        self.runs = {
            f"job-{indice:03d}": [
                {
                    "JobRunState": "SUCCEEDED",
                    "ExecutionTime": 60 * (indice + 1),
                    "StartedOn": inicio,
                    "CompletedOn": inicio + timedelta(minutes=indice + 1),
                }
            ]
            for indice in range(quantidade)
        }

    def get_paginator(self, operation: str):
        assert operation in {"get_jobs", "get_job_runs"}
        if operation == "get_jobs":
            return _GluePaginator(self, [{"Jobs": self.jobs}])
        return _GluePaginator(self, None)


def test_parallel_job_history_matches_the_sequential_result():
    """Paralelizar não pode mudar o dataset — nem a ordem, nem o dono do dado."""
    agora = datetime.now(timezone.utc)
    quantidade = 24
    window = AnalysisWindow.trailing(days=30, now=agora)

    serial = glue_jobs.collect_jobs(
        _Glue(quantidade, agora=agora), window=window, workers=1
    )
    paralelo = glue_jobs.collect_jobs(
        _Glue(quantidade, agora=agora), window=window, workers=8
    )

    assert [job.name for job in paralelo] == [f"job-{i:03d}" for i in range(quantidade)]
    assert [job.name for job in paralelo] == [job.name for job in serial]
    assert [job.avg_execution_sec for job in paralelo] == [
        job.avg_execution_sec for job in serial
    ]
    # Cada job ficou com a própria duração, não com a de um vizinho.
    for indice, job in enumerate(paralelo):
        assert job.avg_execution_sec == 60 * (indice + 1)


def test_one_denied_job_does_not_contaminate_the_others():
    """`GetJobRuns` é permissão à parte e pode ser negado em um job só."""
    agora = datetime.now(timezone.utc)
    quantidade = 12
    window = AnalysisWindow.trailing(days=30, now=agora)
    glue = _Glue(quantidade, agora=agora, negados={"job-004", "job-009"})

    jobs = glue_jobs.collect_jobs(glue, window=window, workers=8)

    assert len(jobs) == quantidade
    negados = {job.name for job in jobs if not job.run_history_available}
    assert negados == {"job-004", "job-009"}
    # E os demais seguem com o próprio histórico.
    for job in jobs:
        if job.name not in negados:
            indice = int(job.name.removeprefix("job-"))
            assert job.avg_execution_sec == 60 * (indice + 1)


class _FakeClient:
    """Cliente que só conta quantas vezes foi chamado."""

    def __init__(self) -> None:
        self.chamadas = 0

    def list_objects_v2(self, **_kwargs):
        self.chamadas += 1
        return {"ResponseMetadata": {"RetryAttempts": 0}}


def test_the_instrumented_client_counts_every_threaded_call():
    """O caminho real: várias threads sobre o mesmo cliente instrumentado."""
    telemetry = RunTelemetry()
    client = InstrumentedClient(_FakeClient(), "s3", telemetry, {})
    threads = 8
    por_thread = 50

    def chamar() -> None:
        for _ in range(por_thread):
            client.list_objects_v2(Bucket="b", Prefix="p")

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for _ in range(threads):
            pool.submit(chamar)

    assert telemetry.api_calls["s3:list_objects_v2"].calls == threads * por_thread


class _AthenaTelemetry:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def unavailable(self, _source, **kwargs) -> None:
        self.problems.append(str(kwargs))

    def failed(self, _source, exc, **_kwargs) -> None:
        self.problems.append(type(exc).__name__)


class _ParallelAthena:
    def __init__(self) -> None:
        self._lock = Lock()
        self.active = 0
        self.peak = 0

    def batch_get_query_execution(self, *, QueryExecutionIds):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        sleep(0.01)
        with self._lock:
            self.active -= 1
        # Inverte a resposta para provar que o coletor restaura a ordem pedida.
        return {
            "QueryExecutions": [
                {"QueryExecutionId": query_id}
                for query_id in reversed(QueryExecutionIds)
            ]
        }


def test_athena_batches_overlap_and_keep_the_query_order():
    client = _ParallelAthena()
    ids = [f"q-{index:03d}" for index in range(200)]

    rows = list(
        athena_executions.query_executions(
            client, ids, _AthenaTelemetry(), workers=4
        )
    )

    assert client.peak > 1
    assert [row["QueryExecutionId"] for row in rows] == ids


class _MetricBatch:
    def __init__(self) -> None:
        self.calls = 0
        self.query_counts: list[int] = []

    def get_metric_data(self, **kwargs):
        self.calls += 1
        queries = kwargs["MetricDataQueries"]
        self.query_counts.append(len(queries))
        return {
            "MetricDataResults": [
                {"Id": query["Id"], "Values": [25.0]}
                for query in queries
            ]
        }


def test_sagemaker_metrics_for_one_hundred_jobs_fit_in_one_initial_call():
    jobs = [
        SageMakerJob(name=f"job-{index:03d}", kind="training")
        for index in range(100)
    ]
    client = _MetricBatch()

    _apply_jobs_metrics(client, jobs, AnalysisWindow.trailing())

    assert client.calls == 1
    assert client.query_counts == [400]
    assert all(job.detailed_metrics and job.cpu_p95 == 25.0 for job in jobs)


class _ParallelSageMaker:
    def __init__(self, count: int, now: datetime) -> None:
        self.count = count
        self.now = now
        self._lock = Lock()
        self.active = 0
        self.peak = 0

    def get_paginator(self, operation: str):
        if operation == "list_training_jobs":
            return _Pages([
                {
                    "TrainingJobSummaries": [
                        {
                            "TrainingJobName": f"job-{index:03d}",
                            "TrainingJobArn": f"arn:job:{index:03d}",
                            "TrainingJobStatus": "Completed",
                            "CreationTime": self.now - timedelta(days=1),
                        }
                        for index in range(self.count)
                    ]
                }
            ])
        if operation == "list_processing_jobs":
            return _Pages([{"ProcessingJobSummaries": []}])
        if operation == "list_transform_jobs":
            return _Pages([{"TransformJobSummaries": []}])
        raise AssertionError(operation)

    def describe_training_job(self, *, TrainingJobName: str):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        sleep(0.01)
        with self._lock:
            self.active -= 1
        return {
            "TrainingJobName": TrainingJobName,
            "TrainingJobStatus": "Completed",
            "CreationTime": self.now - timedelta(days=1),
            "TrainingStartTime": self.now - timedelta(hours=2),
            "TrainingEndTime": self.now - timedelta(hours=1),
            "ResourceConfig": {
                "InstanceType": "ml.m5.large",
                "InstanceCount": 1,
            },
        }


def test_sagemaker_describes_overlap_and_keep_the_listing_order():
    now = datetime.now(timezone.utc)
    client = _ParallelSageMaker(12, now)

    jobs = collect_sagemaker_jobs(
        client,
        window=AnalysisWindow.trailing(now=now),
        detailed_limit=0,
        workers=4,
    )

    assert client.peak > 1
    assert [job.name for job in jobs] == [f"job-{index:03d}" for index in range(12)]


class _ParallelTags:
    def __init__(self) -> None:
        self._lock = Lock()
        self.active = 0
        self.peak = 0

    def list_tags(self, *, ResourceArn: str):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        sleep(0.01)
        with self._lock:
            self.active -= 1
        return {"Tags": [{"Key": "Owner", "Value": ResourceArn.rsplit(":", 1)[-1]}]}


def test_sagemaker_owner_tags_overlap_and_stay_on_the_right_job():
    jobs = [
        SageMakerJob(
            name=f"job-{index:03d}",
            kind="training",
            arn=f"arn:job:owner-{index:03d}",
        )
        for index in range(12)
    ]
    client = _ParallelTags()

    _apply_job_owners(client, jobs, workers=4)

    assert client.peak > 1
    assert [job.owner_tag for job in jobs] == [
        f"owner-{index:03d}" for index in range(12)
    ]


class _Pages:
    def __init__(self, pages) -> None:
        self.pages = pages

    def paginate(self, **_kwargs):
        return iter(self.pages)


class _ParallelStepFunctions:
    def __init__(self, count: int) -> None:
        self.count = count
        self._lock = Lock()
        self.active = 0
        self.peak = 0

    def get_paginator(self, operation: str):
        if operation == "list_state_machines":
            return _Pages([
                {
                    "stateMachines": [
                        {
                            "name": f"machine-{index:03d}",
                            "stateMachineArn": f"arn:machine:{index:03d}",
                        }
                        for index in range(self.count)
                    ]
                }
            ])
        if operation == "list_executions":
            return _Pages([{"executions": []}])
        raise AssertionError(operation)

    def describe_state_machine(self, **_kwargs):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        sleep(0.01)
        with self._lock:
            self.active -= 1
        return {"type": "STANDARD", "definition": "{}"}


def test_stepfunctions_machines_overlap_and_keep_the_listing_order():
    client = _ParallelStepFunctions(12)

    machines = collect_state_machines(
        client, window=AnalysisWindow.trailing(), workers=4
    )

    assert client.peak > 1
    assert [machine.name for machine in machines] == [
        f"machine-{index:03d}" for index in range(12)
    ]
