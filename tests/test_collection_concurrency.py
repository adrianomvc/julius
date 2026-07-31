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
from threading import Barrier
from time import sleep

from julius.collection.collectors.glue import jobs as glue_jobs
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
