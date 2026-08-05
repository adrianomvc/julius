"""Cada investigação explica o próprio problema, e não a mesma frase para nove.

`_investigation` fixava `how_to_apply` e `how_to_validate` no corpo, e nove regras
diferentes saíam com o mesmo par: *"Analisar as execuções referenciadas e testar
uma mudança isolada"* servia para skew, para job sem input e para arquivo pequeno.
A regra sabe o que observou e o que corrige aquilo; o helper é que não sabia.

Nenhuma delas ganha cifra — continuam bloqueadas. O que muda é o leitor saber o
que foi observado e o que fazer a respeito, que é a pergunta que chegou como
*"o que seria esse problema?"*.

O dataset de exemplo não dispara nenhuma destas regras, então sem estes testes a
mudança ficaria sem exercício e o baseline não a pegaria.
"""

from __future__ import annotations

import pytest

from julius.collection.models import Account, GlueJob
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.glue import jobs as glue_jobs

_TEXTO_GENERICO = "Analisar as execuções referenciadas e testar uma mudança isolada."


def _job(**overrides) -> GlueJob:
    base = {
        "name": "etl",
        "glue_version": "4.0",
        "command_type": "glueetl",
        "worker_type": "G.1X",
        "number_of_workers": 10,
        "runs_in_window": 30,
        "observed_runs": 30,
        "coverage_days": 30,
        "window_days": 30,
        "avg_execution_sec": 600.0,
        "dpu_seconds_window": 100000.0,
        "owner_tag": "squad-dados",
    }
    return GlueJob(**{**base, **overrides})


def _achados(job: GlueJob) -> dict[str, object]:
    conta = Account(account_id="123456789012", glue_jobs=[job])
    return {
        item.rule_id: item
        for item in glue_jobs.detect(conta, DEFAULT_CONFIG, "scan")
    }


_CASOS = {
    "GLUE-TASK-SKEW": dict(max_task_skew=8.0),
    "GLUE-NO-INPUT-WASTE": dict(bytes_read_window=0.0),
    "GLUE-SHUFFLE-SPILL": dict(
        shuffle_spill_bytes=5_000_000_000.0, has_spill_evidence=True
    ),
    "GLUE-EXECUTOR-CAPACITY-GAP": dict(
        avg_all_executors=20.0, avg_max_needed_executors=2.0
    ),
    "GLUE-JOB-ABANDONED": dict(
        last_run_at="2024-01-01T00:00:00+00:00", runs_in_window=0, observed_runs=0
    ),
}


@pytest.mark.parametrize("rule_id, campos", _CASOS.items(), ids=list(_CASOS))
def test_each_investigation_says_what_to_do(rule_id, campos):
    achado = _achados(_job(**campos)).get(rule_id)

    assert achado is not None, f"{rule_id} não disparou com {campos}"
    assert achado.how_to_apply != _TEXTO_GENERICO, (
        f"{rule_id} ainda usa o texto genérico do helper"
    )
    assert len(achado.how_to_apply) > 80, (
        f"{rule_id} explica menos que uma frase: {achado.how_to_apply!r}"
    )


@pytest.mark.parametrize("rule_id, campos", _CASOS.items(), ids=list(_CASOS))
def test_each_investigation_says_how_to_check_it_worked(rule_id, campos):
    achado = _achados(_job(**campos))[rule_id]

    assert achado.how_to_validate != "Comparar duração p95, DPU-h e saída antes/depois."


def test_two_investigations_never_share_the_same_text():
    """O defeito era exatamente este: nove regras, um texto."""
    textos = {
        rule_id: _achados(_job(**campos))[rule_id].how_to_apply
        for rule_id, campos in _CASOS.items()
    }

    assert len(set(textos.values())) == len(textos), (
        f"investigações com texto repetido: {textos}"
    )


def test_skew_explains_that_more_workers_do_not_fix_it():
    """É a confusão mais cara desta regra: quem lê "duração desigual" aumenta
    capacidade, e skew não se corrige com worker a mais."""
    achado = _achados(_job(max_task_skew=8.0))["GLUE-TASK-SKEW"]

    assert "skew" in achado.how_to_apply.lower()
    assert "não corrige" in achado.how_to_apply


def test_no_input_says_the_job_paid_without_reading():
    achado = _achados(_job(bytes_read_window=0.0))["GLUE-NO-INPUT-WASTE"]

    assert "sem ler" in achado.how_to_apply


def test_explaining_better_does_not_unblock_the_figure():
    """A fronteira: texto melhor não é evidência, e não vira dinheiro."""
    for rule_id, campos in _CASOS.items():
        achado = _achados(_job(**campos))[rule_id]
        assert achado.blocked is True, rule_id
        assert achado.include_in_portfolio is False, rule_id
