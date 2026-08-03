"""O mecanismo de lote do CloudWatch, usado por sete coletores.

Antes cada coletor pedia uma métrica de um recurso por chamada. O que estes
testes cobram é o contrato do lote: quantas chamadas ele custa, o que acontece
quando uma delas falha, e que "consulta sem valor" continua distinguível de
"chamada que falhou" — porque a primeira é métrica que não existe e a segunda é
evidência que faltou.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from julius.collection.collectors import metrics
from julius.collection.collectors.metrics import (
    MAX_QUERIES_PER_CALL,
    MetricQuery,
)
from julius.collection.telemetry import RunTelemetry

INICIO = datetime(2026, 7, 1, tzinfo=timezone.utc)
FIM = datetime(2026, 7, 31, tzinfo=timezone.utc)


class _CloudWatch:
    def __init__(self, *, falhar_em: set[int] | None = None, paginas: int = 1):
        self.falhar_em = falhar_em or set()
        self.paginas = paginas
        self.chamadas = 0
        self.tamanhos: list[int] = []

    def get_metric_data(self, **kwargs):
        self.chamadas += 1
        if self.chamadas in self.falhar_em:
            raise RuntimeError("ThrottlingException")
        consultas = kwargs["MetricDataQueries"]
        self.tamanhos.append(len(consultas))
        pagina = int(kwargs.get("NextToken") or 0)
        resposta = {
            "MetricDataResults": [
                {"Id": q["Id"], "Values": [1.0], "Timestamps": [INICIO]}
                for q in consultas
            ]
        }
        if pagina + 1 < self.paginas:
            resposta["NextToken"] = str(pagina + 1)
        return resposta


def _consultas(quantidade: int) -> list[MetricQuery]:
    return [
        MetricQuery(namespace="AWS/Teste", metric_name=f"M{i}", stat="Sum")
        for i in range(quantidade)
    ]


def test_many_queries_fit_in_one_call():
    client = _CloudWatch()
    queries = _consultas(120)

    problemas = metrics.collect(client, queries, start=INICIO, end=FIM)

    assert problemas == []
    assert client.chamadas == 1
    assert all(query.values == [1.0] for query in queries)


def test_queries_are_split_at_the_api_ceiling():
    """500 por chamada é limite da API, não escolha nossa."""
    client = _CloudWatch()

    metrics.collect(client, _consultas(1201), start=INICIO, end=FIM)

    assert client.chamadas == 3
    assert client.tamanhos == [MAX_QUERIES_PER_CALL, MAX_QUERIES_PER_CALL, 201]


def test_a_failed_block_is_reported_and_does_not_stop_the_others():
    """O grão do isolamento é o lote; o resto da coleta segue."""
    client = _CloudWatch(falhar_em={1})
    queries = _consultas(600)

    problemas = metrics.collect(client, queries, start=INICIO, end=FIM)

    assert len(problemas) == 1
    # O primeiro lote ficou sem valor; o segundo respondeu.
    assert queries[0].values == []
    assert queries[-1].values == [1.0]


def test_pagination_accumulates_points_of_the_same_query():
    """O teto de pontos por chamada é resolvido por `NextToken`."""
    client = _CloudWatch(paginas=3)
    queries = _consultas(2)

    metrics.collect(client, queries, start=INICIO, end=FIM)

    assert client.chamadas == 3
    assert queries[0].values == [1.0, 1.0, 1.0]
    assert len(queries[0].timestamps) == 3


def test_no_client_is_not_a_failure():
    """Cliente ausente é fonte não configurada, não chamada que deu erro."""
    queries = _consultas(3)

    assert metrics.collect(None, queries, start=INICIO, end=FIM) == []
    assert all(query.values == [] for query in queries)


def test_scan_by_is_forwarded_only_when_asked():
    """Ordem só é pedida por quem precisa do ponto mais recente."""
    registro: list[dict] = []

    class _Registra(_CloudWatch):
        def get_metric_data(self, **kwargs):
            registro.append(kwargs)
            return super().get_metric_data(**kwargs)

    metrics.collect(_Registra(), _consultas(1), start=INICIO, end=FIM)
    assert "ScanBy" not in registro[0]

    metrics.collect(
        _Registra(),
        _consultas(1),
        start=INICIO,
        end=FIM,
        scan_by="TimestampAscending",
    )
    assert registro[1]["ScanBy"] == "TimestampAscending"


def test_points_pairs_timestamps_with_values():
    client = _CloudWatch()
    queries = _consultas(1)

    metrics.collect(client, queries, start=INICIO, end=FIM)

    assert queries[0].points() == [(INICIO, 1.0)]


def test_coordinator_combines_compatible_concurrent_collectors():
    client = _CloudWatch()
    coordinator = metrics.MetricBatchCoordinator(client, coalesce_seconds=0.02)
    client._metric_batch_coordinator = coordinator
    barrier = Barrier(2)
    first = _consultas(120)
    second = _consultas(80)

    def run(queries):
        barrier.wait()
        return metrics.collect(client, queries, start=INICIO, end=FIM)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first, second)))

    assert results == [[], []]
    assert client.chamadas == 1
    assert client.tamanhos == [200]
    assert all(query.values == [1.0] for query in first + second)


def test_coordinator_does_not_mix_different_scan_order():
    client = _CloudWatch()
    coordinator = metrics.MetricBatchCoordinator(client, coalesce_seconds=0.02)
    client._metric_batch_coordinator = coordinator
    barrier = Barrier(2)

    def run(scan_by):
        barrier.wait()
        return metrics.collect(
            client,
            _consultas(1),
            start=INICIO,
            end=FIM,
            scan_by=scan_by,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ("", "TimestampAscending")))

    assert client.chamadas == 2


def test_coordinator_reports_failed_shared_block_to_each_collector():
    client = _CloudWatch(falhar_em={1})
    coordinator = metrics.MetricBatchCoordinator(client, coalesce_seconds=0.02)
    client._metric_batch_coordinator = coordinator
    barrier = Barrier(2)

    def run(_):
        barrier.wait()
        return metrics.collect(client, _consultas(1), start=INICIO, end=FIM)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (1, 2)))

    assert [len(problems) for problems in results] == [1, 1]


def test_coordinator_records_logical_requests_and_physical_batches():
    client = _CloudWatch()
    telemetry = RunTelemetry()
    coordinator = metrics.MetricBatchCoordinator(
        client,
        telemetry=telemetry,
        coalesce_seconds=0.02,
    )
    client._metric_batch_coordinator = coordinator
    barrier = Barrier(2)

    def run(amount):
        barrier.wait()
        return metrics.collect(client, _consultas(amount), start=INICIO, end=FIM)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, (300, 300)))

    assert telemetry.cloudwatch_metric_requests == 2
    assert telemetry.cloudwatch_metric_queries == 600
    assert telemetry.cloudwatch_metric_batches == 2
    assert telemetry.cloudwatch_coalesced_requests == 2
