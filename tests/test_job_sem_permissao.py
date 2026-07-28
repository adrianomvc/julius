"""Um job sem permissão não pode derrubar a conta inteira.

`GetJobRuns` é permissão separada de `GetJobs`, e pode ser negada em **um** job
— política de recurso, Lake Formation, tag de restrição. O coletor pedia o
histórico job a job sem isolamento, então esse `AccessDenied` subia até o
`recorder`; e como `Glue Jobs` é a única fonte obrigatória, a coleta abortava.
Uma conta com trezentos jobs ficava sem scan por causa de um.

O que este arquivo cobra não é só que a coleta sobrevive. É que o job
sobrevivente **não mente**: a configuração dele veio do `GetJobs` e continua
válida, mas as execuções não foram medidas, e zero-por-falta-de-permissão não
pode virar o mesmo zero de quem simplesmente não rodou.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from julius.collection import sources as collect_module
from julius.collection.collectors.glue import jobs as glue_collector
from julius.collection.models import Account, CollectionHealth
from julius.collection.window import AnalysisWindow

_JANELA = AnalysisWindow.trailing(now=datetime(2026, 7, 24, tzinfo=timezone.utc))


class _AcessoNegado(Exception):
    response = {"Error": {"Code": "AccessDeniedException", "Message": "negado"}}


class _GlueComUmJobBloqueado:
    """Lista dois jobs; nega o histórico de um deles."""

    def __init__(self, bloqueado: str | None):
        #: `None` = nenhum job bloqueado, para o teste de contraste existir.
        self.bloqueado = bloqueado
        self.consultados: list[str] = []

    def get_paginator(self, operacao: str):
        cliente = self

        class _Pages:
            def paginate(self, **kwargs):
                if operacao == "get_jobs":
                    nomes = ["livre"]
                    if cliente.bloqueado:
                        nomes.append(cliente.bloqueado)
                    yield {
                        "Jobs": [
                            {
                                "Name": nome,
                                "GlueVersion": "5.1",
                                "WorkerType": "G.1X",
                                "NumberOfWorkers": 2,
                                "DefaultArguments": {},
                                "Command": {"Name": "glueetl"},
                            }
                            for nome in nomes
                        ]
                    }
                    return
                nome = kwargs["JobName"]
                cliente.consultados.append(nome)
                if nome == cliente.bloqueado:
                    raise _AcessoNegado()
                yield {
                    "JobRuns": [
                        {
                            "Id": "jr-1",
                            "JobRunState": "SUCCEEDED",
                            "ExecutionTime": 600,
                            "DPUSeconds": 1200.0,
                            "StartedOn": _JANELA.start,
                        }
                    ]
                }

        return _Pages()


def test_one_forbidden_job_does_not_abort_the_collection():
    """Era o bug: a conta inteira ficava sem scan por causa de um recurso."""
    glue = _GlueComUmJobBloqueado("restrito")

    jobs = glue_collector.collect_jobs(glue, window=_JANELA)

    assert sorted(job.name for job in jobs) == ["livre", "restrito"]
    # O histórico do job livre foi lido mesmo com o outro negando.
    assert glue.consultados == ["livre", "restrito"]


def test_the_forbidden_job_keeps_the_configuration_that_was_readable():
    """`GetJobs` passou: worker type, versão e capacidade continuam válidos."""
    jobs = glue_collector.collect_jobs(
        _GlueComUmJobBloqueado("restrito"), window=_JANELA
    )
    restrito = next(job for job in jobs if job.name == "restrito")

    assert restrito.worker_type == "G.1X"
    assert restrito.number_of_workers == 2
    assert restrito.glue_version == "5.1"


def test_unmeasured_is_not_the_same_zero_as_never_ran():
    """A distinção que impede um job não medido de virar conclusão."""
    jobs = glue_collector.collect_jobs(
        _GlueComUmJobBloqueado("restrito"), window=_JANELA
    )
    livre = next(job for job in jobs if job.name == "livre")
    restrito = next(job for job in jobs if job.name == "restrito")

    assert livre.run_history_available is True
    assert restrito.run_history_available is False
    assert restrito.observed_runs == 0
    assert restrito.total_dpu_hours_window == 0.0


def test_the_degradation_reaches_the_collection_health():
    """Sem isto, os zeros do job negado passariam por medição."""
    entrada = CollectionHealth(source="Glue Jobs", status="ok")
    contexto = collect_module.CollectionContext(
        session=object(),
        window=_JANELA,
        billing=collect_module.BillingMonth.current(),
        account=Account(account_id="1"),
        config=object(),
    )
    jobs = glue_collector.collect_jobs(
        _GlueComUmJobBloqueado("restrito"), window=_JANELA
    )

    collect_module._record_jobs_integrity(contexto, jobs, entrada)

    assert entrada.status == "partial"
    assert entrada.error_category == "permission_denied"
    assert "1 job(s) sem histórico" in entrada.impact
    assert "glue:GetJobRuns" in entrada.next_action
    # Fonte degradada não conta como inventário íntegro para o rateio de custo.
    assert contexto.flags["jobs_collection_complete"] is False


def test_an_account_where_everything_is_readable_stays_ok():
    """O contraste: sem ele, o teste acima passaria com a regra sempre ligada."""
    entrada = CollectionHealth(source="Glue Jobs", status="ok")
    contexto = collect_module.CollectionContext(
        session=object(),
        window=_JANELA,
        billing=collect_module.BillingMonth.current(),
        account=Account(account_id="1"),
        config=object(),
    )
    jobs = glue_collector.collect_jobs(_GlueComUmJobBloqueado(None), window=_JANELA)

    collect_module._record_jobs_integrity(contexto, jobs, entrada)

    assert [job.name for job in jobs] == ["livre"]
    assert entrada.status == "ok"
    assert contexto.flags["jobs_collection_complete"] is True


@pytest.mark.parametrize("codigo", ["AccessDeniedException", "EntityNotFoundException"])
def test_any_failure_on_the_history_is_isolated_not_only_permission(codigo):
    """Um job apagado no meio da coleta não é diferente de um job negado."""

    class _Erro(Exception):
        response = {"Error": {"Code": codigo}}

    class _Glue(_GlueComUmJobBloqueado):
        def get_paginator(self, operacao):
            pai = super().get_paginator(operacao)
            if operacao == "get_jobs":
                return pai

            class _Pages:
                def paginate(self, **kwargs):
                    raise _Erro()

            return _Pages()

    jobs = glue_collector.collect_jobs(_Glue("restrito"), window=_JANELA)

    assert len(jobs) == 2
    assert all(job.run_history_available is False for job in jobs)
