"""Métricas do CloudWatch em lote, para qualquer namespace.

`GetMetricStatistics` responde uma métrica de um recurso por chamada. Coletar
três métricas de vinte clusters Redshift são sessenta idas à AWS em série, cada
uma pagando a latência inteira — e o mesmo padrão se repetia em sete coletores.
`GetMetricData` aceita **500 consultas por chamada**, então as mesmas sessenta
cabem em uma.

O mecanismo nasceu dentro do coletor de Glue, que tinha o caso extremo (onze
métricas × trezentos jobs = 3.300 consultas em sete chamadas), mas estava preso
lá: o namespace era constante do módulo e as dimensões eram fixas em `JobName`.
Aqui ele é dado.

Este módulo **não reduz**: preenche `MetricQuery.values` e devolve. Reduzir é do
chamador porque as reduções não são intercambiáveis — o Redshift precisa da média
e também de `len()` para saber quantos dias observou, e uma API que só devolvesse
"um número por métrica" perderia isso.

Duas coisas que não mudam ao sair do `GetMetricStatistics`:

- `Maximum` continua `Maximum`. Não suavizar pico de memória, disco e skew é
  decisão de comportamento — o gate de capacidade precisa do pior pico da janela,
  não da média dele.
- Ausência de métrica continua lista vazia, e o chamador a traduz em `None`.
  Zero significaria "medido e vazio", que é afirmação diferente.

Ordem: o `GetMetricData` devolve por timestamp **descendente** por default. A
maioria das reduções não se importa — média, máximo, soma, contagem — mas há
quem precise do ponto mais recente (o tamanho atual de um bucket S3 é o último
ponto da série, não um agregado). Para esses, `scan_by` mapeia direto o parâmetro
da API, e é do lote inteiro porque na API ele também é: consultas com ordens
diferentes não cabem na mesma chamada.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock
from time import sleep
from typing import Any, TypeVar

#: Teto da API para consultas numa única chamada `GetMetricData`.
MAX_QUERIES_PER_CALL = 500

#: Um ponto por dia. Período diário é o que mantém a série disponível por 455
#: dias no CloudWatch — o corte de 63 dias vale para período de cinco minutos.
DAILY_PERIOD_SECONDS = 86400

# Janela curta para fontes que chegaram juntas ao mesmo cliente CloudWatch.
# Dez milissegundos ficam muito abaixo da latência normal da API e permitem que
# workers liberados pelo mesmo nível do DAG compartilhem a chamada.
COALESCE_WINDOW_SECONDS = 0.01
_BlockItem = TypeVar("_BlockItem")


@dataclass
class MetricQuery:
    """Uma consulta pendente e os pontos que ela trouxe."""

    namespace: str
    metric_name: str
    stat: str
    #: Dimensões completas e já resolvidas, na ordem em que a API as recebe.
    dimensions: tuple[tuple[str, str], ...] = ()
    period: int = DAILY_PERIOD_SECONDS
    values: list[float] = field(default_factory=list)
    #: Alinhado com `values` por índice. Preenchido sempre, porque custa nada — a
    #: API devolve os dois lados — e há quem precise do instante e não do
    #: agregado: "última invocação" é o timestamp do último ponto não nulo.
    timestamps: list[object] = field(default_factory=list)

    def points(self) -> list[tuple[object, float]]:
        """`(timestamp, valor)` na ordem em que a API respondeu."""
        return list(zip(self.timestamps, self.values, strict=False))


@dataclass
class _MetricRequest:
    queries: list[MetricQuery]
    done: Event = field(default_factory=Event)
    problems: list[Exception] = field(default_factory=list)


class MetricBatchCoordinator:
    """Agrupa chamadas concorrentes e compatíveis de fontes diferentes.

    O coordenador vive no cliente CloudWatch compartilhado pelo scan. Uma
    requisição só se mistura com outra quando janela e ``ScanBy`` são idênticos;
    isso preserva retenção, ordem temporal e redução de cada coletor. O chamador
    continua bloqueado até suas próprias consultas terminarem, portanto nenhum
    objeto parcialmente preenchido escapa para o agregador.
    """

    def __init__(
        self,
        cw_client,
        *,
        telemetry: Any = None,
        coalesce_seconds: float = COALESCE_WINDOW_SECONDS,
    ) -> None:
        self._client = cw_client
        self._telemetry = telemetry
        self._coalesce_seconds = max(0.0, coalesce_seconds)
        self._lock = Lock()
        self._pending: dict[tuple[datetime, datetime, str], list[_MetricRequest]] = {}
        self._active: set[tuple[datetime, datetime, str]] = set()

    def collect(
        self,
        queries: list[MetricQuery],
        *,
        start: datetime,
        end: datetime,
        scan_by: str = "",
    ) -> list[Exception]:
        request = _MetricRequest(queries)
        key = (start, end, scan_by)
        with self._lock:
            self._pending.setdefault(key, []).append(request)
            leader = key not in self._active
            if leader:
                self._active.add(key)

        if leader:
            self._drain(key)
        else:
            request.done.wait()
        return request.problems

    def _drain(self, key: tuple[datetime, datetime, str]) -> None:
        if self._coalesce_seconds:
            sleep(self._coalesce_seconds)
        while True:
            with self._lock:
                requests = self._pending.pop(key, [])
            if requests:
                self._execute(requests, key)
                continue
            with self._lock:
                # Uma requisição pode ter chegado entre o primeiro teste e a
                # aquisição deste lock. Nesse caso o líder permanece responsável.
                if self._pending.get(key):
                    continue
                self._active.remove(key)
                return

    def _execute(
        self,
        requests: list[_MetricRequest],
        key: tuple[datetime, datetime, str],
    ) -> None:
        start, end, scan_by = key
        indexed = [
            (request, query)
            for request in requests
            for query in request.queries
        ]
        if self._telemetry is not None:
            self._telemetry.record_cloudwatch_batch(
                requests=len(requests),
                queries=len(indexed),
                coalesced=len(requests) if len(requests) > 1 else 0,
            )
        for block in _blocks(indexed, MAX_QUERIES_PER_CALL):
            if self._telemetry is not None:
                self._telemetry.record_cloudwatch_batch(batches=1)
            try:
                _fetch(
                    self._client,
                    [query for _, query in block],
                    start,
                    end,
                    scan_by,
                )
            except Exception as exc:  # noqa: BLE001 - isolado por lote
                affected = {id(request): request for request, _ in block}
                for request in affected.values():
                    request.problems.append(exc)
        for request in requests:
            request.done.set()


def collect(
    cw_client,
    queries: list[MetricQuery],
    *,
    start: datetime,
    end: datetime,
    scan_by: str = "",
) -> list[Exception]:
    """Preenche `values` de cada consulta, em lotes de `MAX_QUERIES_PER_CALL`.

    Devolve as falhas, uma por lote que não completou. Lista vazia é sucesso.
    Devolver em vez de engolir importa porque "consulta sem valor" e "chamada que
    falhou" são afirmações diferentes: a primeira é métrica que não existe, a
    segunda é evidência que faltou — e há chamador que precisa reportar a
    diferença em vez de tratar as duas como ausência.

    O isolamento de falha tem o grão do lote: uma chamada ruim deixa as consultas
    daquele lote vazias. Antes, com uma chamada por métrica, o grão era a métrica.

    O teto de 100.800 pontos por chamada é resolvido pelo `NextToken`, não pelo
    tamanho do lote: com período horário, 500 consultas de 30 dias passam do teto
    e a API responde em páginas, que este laço acumula por consulta.
    """
    if cw_client is None:
        return []
    coordinator = getattr(cw_client, "_metric_batch_coordinator", None)
    if coordinator is not None:
        return coordinator.collect(
            queries,
            start=start,
            end=end,
            scan_by=scan_by,
        )
    problems: list[Exception] = []
    for block in _blocks(queries, MAX_QUERIES_PER_CALL):
        try:
            _fetch(cw_client, block, start, end, scan_by)
        except Exception as exc:  # noqa: BLE001 - o chamador decide o que reportar
            problems.append(exc)
    return problems


def _blocks(queries: list[_BlockItem], size: int) -> Iterator[list[_BlockItem]]:
    for começo in range(0, len(queries), size):
        yield queries[começo : começo + size]


def _fetch(
    cw_client,
    block: list[MetricQuery],
    start: datetime,
    end: datetime,
    scan_by: str = "",
) -> None:
    """Executa um lote e acumula os valores por consulta, página a página."""
    por_id = {f"m{indice}": query for indice, query in enumerate(block)}
    payload = [
        {
            "Id": query_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": query.namespace,
                    "MetricName": query.metric_name,
                    "Dimensions": [
                        {"Name": nome, "Value": valor}
                        for nome, valor in query.dimensions
                    ],
                },
                "Period": query.period,
                "Stat": query.stat,
            },
            "ReturnData": True,
        }
        for query_id, query in por_id.items()
    ]

    token = None
    while True:
        kwargs: dict = {
            "MetricDataQueries": payload,
            "StartTime": start,
            "EndTime": end,
        }
        if scan_by:
            kwargs["ScanBy"] = scan_by
        if token:
            kwargs["NextToken"] = token
        response = cw_client.get_metric_data(**kwargs)
        for result in response.get("MetricDataResults", []):
            query = por_id.get(str(result.get("Id")))
            if query is None:
                continue
            query.values.extend(float(valor) for valor in result.get("Values", []))
            query.timestamps.extend(result.get("Timestamps", []))
        token = response.get("NextToken")
        if not token:
            return
