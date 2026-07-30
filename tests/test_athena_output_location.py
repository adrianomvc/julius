"""Recuperar execuções do Athena quando `ListQueryExecutions` é negado.

`ListQueryExecutions` é permissão à parte de `BatchGetQueryExecution`. Negá-la
zerava o workgroup inteiro: sem IDs não havia o que buscar, e o relatório
afirmava que ninguém usa o Athena da conta. Mas os IDs estão nos nomes dos
resultados gravados no output location — que o Julius já conhece, porque a
listagem do S3 é derivada dele — e o `ProcessedBytes` vem da resposta da própria
API, não do CloudWatch. Recuperar o ID por outro caminho devolve a medição
inteira.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from julius.collection.collectors.athena.monthly import collect_analysis

MB = 1024 * 1024
AGORA = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
ONTEM = AGORA - timedelta(days=1)

IDS = [
    "11111111-2222-3333-4444-555555555555",
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
]


def _execucao(query_id: str, *, bytes_lidos: int = 10 * MB) -> dict:
    return {
        "QueryExecutionId": query_id,
        "WorkGroup": "primary",
        "Query": "SELECT cliente FROM vendas.pedidos",
        "StatementType": "DML",
        "Status": {"State": "SUCCEEDED", "SubmissionDateTime": ONTEM},
        "Statistics": {"DataScannedInBytes": bytes_lidos},
        "ResultConfiguration": {"OutputLocation": "s3://resultados/primary/"},
    }


class _Athena:
    """Athena que responde `BatchGet` mas nega a listagem, se pedido."""

    def __init__(self, execucoes: list[dict], *, nega_listagem: bool = False):
        self.execucoes = execucoes
        self.nega_listagem = nega_listagem
        self.listagens = 0

    def get_paginator(self, name):
        if name == "list_work_groups":
            return _Paginator(lambda **_: [{"WorkGroups": [{"Name": "primary"}]}])
        if name == "list_query_executions":
            return _Paginator(self._listar)
        raise AssertionError(name)

    def _listar(self, **_kwargs):
        self.listagens += 1
        if self.nega_listagem:
            raise PermissionError("AccessDeniedException")
        return [{"QueryExecutionIds": [e["QueryExecutionId"] for e in self.execucoes]}]

    def get_work_group(self, WorkGroup):
        return {
            "WorkGroup": {
                "Name": WorkGroup,
                "Configuration": {
                    "PublishCloudWatchMetricsEnabled": True,
                    "ResultConfiguration": {
                        "OutputLocation": "s3://resultados/primary/"
                    },
                },
            }
        }

    def batch_get_query_execution(self, QueryExecutionIds):
        return {
            "QueryExecutions": [
                item
                for item in self.execucoes
                if item["QueryExecutionId"] in QueryExecutionIds
            ]
        }


class _Paginator:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kwargs):
        return self.fn(**kwargs)


class _S3:
    """S3 com os resultados gravados pelo Athena."""

    def __init__(self, chaves: list[str], *, falha: bool = False):
        self.chaves = chaves
        self.falha = falha
        self.chamadas = 0

    def list_objects_v2(self, **kwargs):
        self.chamadas += 1
        if self.falha:
            raise PermissionError("AccessDeniedException")
        return {
            "Contents": [
                {"Key": chave, "Size": 100, "LastModified": ONTEM}
                for chave in self.chaves
            ],
            "IsTruncated": False,
        }


def _chaves_de(ids: list[str]) -> list[str]:
    """Como o Athena grava: SELECT vira `.csv` + `.csv.metadata`."""
    return [f"primary/{i}.csv" for i in ids] + [
        f"primary/{i}.csv.metadata" for i in ids
    ]


def test_denied_listing_falls_back_to_the_result_objects():
    athena = _Athena([_execucao(i) for i in IDS], nega_listagem=True)
    s3 = _S3(_chaves_de(IDS))

    analise = collect_analysis(athena, s3_client=s3, now=AGORA)

    assert analise.coverage.execution_source == {"primary": "output_location"}
    assert analise.coverage.workgroups_covered == 1
    # A medição é a mesma: os bytes vêm da resposta da API, não do CloudWatch.
    assert analise.coverage.api_scanned_bytes == 20 * MB


def test_the_working_listing_does_not_touch_s3():
    """Fallback é fallback: não custa request de LIST quando não é preciso."""
    athena = _Athena([_execucao(i) for i in IDS])
    s3 = _S3(_chaves_de(IDS))

    analise = collect_analysis(athena, s3_client=s3, now=AGORA)

    assert analise.coverage.execution_source == {"primary": "listing"}
    assert s3.chamadas == 0
    assert analise.coverage.api_scanned_bytes == 20 * MB


def test_both_paths_measure_the_same_bytes():
    """A origem muda o alcance, não a medição do que foi alcançado."""
    execucoes = [_execucao(i) for i in IDS]

    pela_listagem = collect_analysis(_Athena(execucoes), now=AGORA)
    pelo_output = collect_analysis(
        _Athena(execucoes, nega_listagem=True),
        s3_client=_S3(_chaves_de(IDS)),
        now=AGORA,
    )

    assert (
        pela_listagem.coverage.api_scanned_bytes
        == pelo_output.coverage.api_scanned_bytes
    )
    assert len(pela_listagem.queries) == len(pelo_output.queries)


def test_every_result_object_shape_maps_to_one_id():
    """`.csv`, `.csv.metadata`, `.txt` de DDL e subpasta de data são o mesmo ID."""
    athena = _Athena([_execucao(IDS[0])], nega_listagem=True)
    s3 = _S3(
        [
            f"primary/{IDS[0]}.csv",
            f"primary/{IDS[0]}.csv.metadata",
            f"primary/{IDS[0]}.txt",
            f"primary/2026/07/30/{IDS[0]}",
        ]
    )

    analise = collect_analysis(athena, s3_client=s3, now=AGORA)

    assert analise.coverage.workgroups_covered == 1
    assert analise.coverage.api_scanned_bytes == 10 * MB


def test_without_s3_the_workgroup_stays_uncovered_instead_of_silent():
    """Sem cliente S3 não há fallback — e isso não pode virar 'zero queries'."""
    athena = _Athena([_execucao(i) for i in IDS], nega_listagem=True)

    analise = collect_analysis(athena, now=AGORA)

    assert analise.coverage.workgroups_covered == 0
    assert analise.coverage.execution_source == {}
    assert analise.queries == []


def test_a_failed_listing_of_the_output_location_is_not_an_empty_athena():
    """Listagem negada nos dois caminhos deixa o workgroup fora da cobertura."""
    athena = _Athena([_execucao(i) for i in IDS], nega_listagem=True)

    analise = collect_analysis(athena, s3_client=_S3([], falha=True), now=AGORA)

    assert analise.coverage.workgroups_covered == 0
    assert analise.coverage.execution_source == {}


def test_a_capped_listing_marks_the_coverage_as_truncated():
    """Cortar no teto é 'pode haver mais', nunca 'era só isso'."""
    athena = _Athena([_execucao(i) for i in IDS], nega_listagem=True)

    analise = collect_analysis(
        athena,
        s3_client=_S3(_chaves_de(IDS)),
        max_ids_per_workgroup=1,
        now=AGORA,
    )

    assert analise.coverage.truncated is True
    assert analise.coverage.execution_source == {"primary": "output_location"}
