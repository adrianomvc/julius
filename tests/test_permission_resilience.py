"""Uma permissão negada tira o recurso, não o serviço inteiro.

O SSO com que o Julius roda é restrito de propósito, e negar uma operação
pontual é o normal, não a exceção: política de recurso, Lake Formation, tag de
restrição. O que não pode acontecer é a negação de **um** recurso apagar todos
os outros do mesmo serviço — porque o relatório então afirma que a conta não usa
Step Functions, não tem crawler, não tem tabela. Afirmar isso a partir de uma
permissão faltando é pior do que não afirmar nada.

O defeito que tornava isso possível estava escrito igual em quatro coletores:

    try:
        pages = client.get_paginator("x").paginate()   # lazy: não chama nada
    except Exception:
        return []
    for page in pages:                                 # a chamada é aqui
        ...

O `except` cobria a linha que não fazia requisição. Estes testes usam
`botocore.stub.Stubber` para negar de verdade e cobram o comportamento, não a
forma do bloco — refatorar o coletor é livre, apagar o isolamento não é.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber

from julius.collection.collectors import schedules as schedules_collector
from julius.collection.collectors import stepfunctions as sfn_collector
from julius.collection.collectors.glue import crawlers as crawlers_collector
from julius.collection.collectors.glue import jobs as jobs_collector
from julius.collection.collectors.paginate import safe_call, safe_pages
from julius.collection.window import AnalysisWindow

AGORA = datetime(2026, 7, 29, tzinfo=timezone.utc)


@pytest.fixture
def janela() -> AnalysisWindow:
    return AnalysisWindow(start=AGORA - timedelta(days=30), end=AGORA, days=30)


def _client(service: str):
    return boto3.client(
        service,
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _negar(stub: Stubber, operacao: str, expected_params=None) -> None:
    stub.add_client_error(
        operacao,
        service_error_code="AccessDeniedException",
        service_message="not authorized",
        http_status_code=400,
        expected_params=expected_params,
    )


# ---------------------------------------------------------------------------
# O helper
# ---------------------------------------------------------------------------


def test_wrapping_only_the_paginate_call_catches_nothing():
    """O defeito, reproduzido: um teste que nunca falhou não prova nada.

    Se algum dia `paginate()` passar a fazer a requisição, este teste falha e
    avisa que a razão de existir do helper mudou.
    """
    glue = _client("glue")
    with Stubber(glue) as stub:
        _negar(stub, "get_crawlers")
        try:
            pages = glue.get_paginator("get_crawlers").paginate()
        except Exception:  # pragma: no cover - é o ponto: não passa por aqui
            pytest.fail("paginate() passou a fazer a chamada; o helper mudou de razão")
        with pytest.raises(Exception, match="AccessDenied"):
            list(pages)


def test_a_denied_pagination_is_caught_where_the_call_actually_happens():
    glue = _client("glue")
    with Stubber(glue) as stub:
        _negar(stub, "get_crawlers")
        resultado = safe_pages(glue, "get_crawlers", "Crawlers")

    assert resultado.items == []
    assert resultado.complete is False
    # A categoria é o que separa "faltou permissão" de "não havia nada".
    assert resultado.error_category == "permission_denied"


def test_what_was_read_before_the_failure_is_not_thrown_away():
    """Evidência truncada nunca vira zero, nem quando a falha é no meio."""
    glue = _client("glue")
    with Stubber(glue) as stub:
        stub.add_response(
            "get_crawlers",
            {"Crawlers": [{"Name": "primeiro"}], "NextToken": "t"},
            {},
        )
        _negar(stub, "get_crawlers", {"NextToken": "t"})
        resultado = safe_pages(glue, "get_crawlers", "Crawlers")

    assert [item["Name"] for item in resultado.items] == ["primeiro"]
    assert resultado.complete is False
    assert resultado.error_category == "permission_denied"


def test_safe_call_isolates_a_single_resource():
    sfn = _client("stepfunctions")
    with Stubber(sfn) as stub:
        _negar(stub, "describe_state_machine")
        resposta, falha = safe_call(
            sfn,
            "describe_state_machine",
            stateMachineArn="arn:aws:states:sa-east-1:123456789012:stateMachine:x",
        )

    assert resposta == {}
    assert falha == "permission_denied"


def test_an_operation_the_installed_botocore_does_not_know_is_not_a_crash():
    """O `getattr` também precisa estar protegido.

    APIs novas — `GetBucketMetadataConfiguration`, por exemplo — não existem em
    botocore antigo, e um cliente boto3 levanta `AttributeError` ao resolver o
    método. Resolvê-lo fora do `try` deixaria esse erro escapar justamente no
    caso em que ele acontece.
    """
    resposta, falha = safe_call(_client("s3"), "operacao_que_nao_existe", Bucket="x")

    assert resposta == {}
    assert falha == "service_error"


# ---------------------------------------------------------------------------
# Os coletores
# ---------------------------------------------------------------------------


def test_a_denied_crawler_history_does_not_erase_the_other_crawlers(janela):
    glue = _client("glue")
    with Stubber(glue) as stub:
        stub.add_response(
            "get_crawlers",
            {"Crawlers": [{"Name": "negado"}, {"Name": "visivel"}]},
            {},
        )
        _negar(stub, "get_crawler_metrics")
        _negar(stub, "list_crawls", {"CrawlerName": "negado"})
        stub.add_response("list_crawls", {"Crawls": []}, {"CrawlerName": "visivel"})

        gaps: list[str] = []
        crawlers = crawlers_collector.collect_crawlers(
            glue, window=janela, gaps=gaps
        )

    assert [item.name for item in crawlers] == ["negado", "visivel"]
    assert any("list_crawls: permission_denied" in gap for gap in gaps)


def test_a_database_denied_by_lake_formation_does_not_erase_the_catalog():
    """O catálogo é o que define o escopo de S3: zerá-lo zera duas análises."""
    glue = _client("glue")
    with Stubber(glue) as stub:
        _negar(stub, "get_tables", {"DatabaseName": "negado"})
        stub.add_response(
            "get_tables",
            {
                "TableList": [
                    {
                        "Name": "vendas",
                        "StorageDescriptor": {"Location": "s3://lake/vendas/"},
                    }
                ]
            },
            {"DatabaseName": "visivel"},
        )

        gaps: list[str] = []
        tabelas = jobs_collector.collect_tables(
            glue, ["negado", "visivel"], gaps=gaps
        )

    assert [t.name for t in tabelas] == ["visivel.vendas"]
    assert tabelas[0].location == "s3://lake/vendas/"
    assert gaps == ["get_tables[negado]: permission_denied"]


def test_a_denied_state_machine_stays_in_the_inventory_saying_what_is_missing(janela):
    sfn = _client("stepfunctions")
    arn = "arn:aws:states:sa-east-1:123456789012:stateMachine:negada"
    with Stubber(sfn) as stub:
        stub.add_response(
            "list_state_machines",
            {
                "stateMachines": [
                    {
                        "stateMachineArn": arn,
                        "name": "negada",
                        "type": "STANDARD",
                        "creationDate": AGORA,
                    }
                ]
            },
            {},
        )
        _negar(stub, "describe_state_machine", {"stateMachineArn": arn})
        _negar(stub, "list_executions", {"stateMachineArn": arn})

        gaps: list[str] = []
        machines = sfn_collector.collect_state_machines(
            sfn, window=janela, gaps=gaps
        )

    assert [m.name for m in machines] == ["negada"]
    maquina = machines[0]
    # Ela existe, e o inventário diz o que não foi lido dela — em vez de sumir.
    assert maquina.definition_available is False
    assert maquina.execution_history_available is False
    assert maquina.avg_state_transitions is None
    assert any("describe_state_machine: permission_denied" in gap for gap in gaps)


def test_a_denied_rule_does_not_erase_the_other_schedules():
    events = _client("events")
    alvo = "arn:aws:states:sa-east-1:123456789012:stateMachine:diario"
    with Stubber(events) as stub:
        stub.add_response(
            "list_rules",
            {
                "Rules": [
                    {"Name": "negada", "ScheduleExpression": "rate(1 day)", "State": "ENABLED"},
                    {"Name": "visivel", "ScheduleExpression": "rate(1 day)", "State": "ENABLED"},
                ]
            },
            {},
        )
        _negar(stub, "list_targets_by_rule", {"Rule": "negada"})
        stub.add_response(
            "list_targets_by_rule",
            {"Targets": [{"Id": "1", "Arn": alvo}]},
            {"Rule": "visivel"},
        )

        gaps: list[str] = []
        agendas = schedules_collector.collect_schedules(events, gaps=gaps)

    assert [item.name for item in agendas] == ["visivel"]
    assert gaps == ["list_targets_by_rule: permission_denied"]
