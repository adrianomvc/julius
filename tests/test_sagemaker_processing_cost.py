"""Custo de processing job do SageMaker.

Nenhum teste cobria processing job — é a mesma classe de buraco que deixou
`collect_buckets` passar sem cobertura. O sintoma que trouxe o assunto foi
`sm-processing failed cost` com `allocated_cost` nulo e `modeled_cost` perto de
zero, e a investigação mostrou que são dois defeitos diferentes com o mesmo
silêncio: describe negado e job que não chegou a iniciar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from julius.collection.collectors.sagemaker_cost import allocate_costs
from julius.collection.collectors.sagemaker_extended import (
    _apply_modeled_cost,
    _normalize_job,
)
from julius.collection.models import Account, SageMakerCostCoverage
from julius.collection.window import AnalysisWindow
from julius.config import DEFAULT_CONFIG

AGORA = datetime(2026, 7, 30, tzinfo=timezone.utc)
INICIO = AGORA - timedelta(days=2)
JANELA = AnalysisWindow.trailing(days=30, now=AGORA)

#: Preço conhecido para o tipo usado, para o teste medir a fórmula e não a tabela.
PRICING = DEFAULT_CONFIG.pricing


def _cru(nome: str, **extra) -> dict:
    base = {
        "ProcessingJobName": nome,
        "ProcessingJobStatus": "Failed",
        "CreationTime": INICIO,
        "ProcessingEndTime": INICIO + timedelta(minutes=30),
        "FailureReason": "AlgorithmError",
        "ProcessingResources": {
            "ClusterConfig": {"InstanceType": "ml.m5.xlarge", "InstanceCount": 2}
        },
    }
    base.update(extra)
    return base


def _job(nome: str, **extra):
    job = _normalize_job("processing", _cru(nome, **extra), JANELA, JANELA)
    _apply_modeled_cost(job, PRICING, "processing")
    return job


def test_a_job_that_ran_and_failed_has_billable_hours():
    """O caso normal: falhou depois de meia hora em duas instâncias."""
    job = _job("rodou", ProcessingStartTime=INICIO)

    assert job.instance_type == "ml.m5.xlarge"
    assert job.instance_count == 2
    assert job.instance_hours == 1.0
    assert job.cost_quality == "modeled"
    assert job.modeled_cost is not None and job.modeled_cost > 0


def test_a_denied_describe_says_so_instead_of_costing_zero():
    """Sem describe, o summary não traz tipo nem contagem de instância.

    O job entrava no inventário com zeros e sem motivo — indistinguível de um
    job que custou nada.
    """
    cru = {
        "ProcessingJobName": "negado",
        "ProcessingJobStatus": "Failed",
        "CreationTime": INICIO,
        "ProcessingEndTime": INICIO + timedelta(minutes=30),
    }
    job = _normalize_job("processing", cru, JANELA, JANELA)
    _apply_modeled_cost(job, PRICING, "processing")

    assert job.cost_quality == "unavailable"
    assert "configuração de recurso" in job.cost_unavailable_reason
    assert job.modeled_cost is None


def test_a_job_that_never_started_is_unavailable_not_a_measured_zero():
    """Zero é verdade aqui — mas `modeled` sobre zero afirmaria medição."""
    job = _job("nao_iniciou")  # sem ProcessingStartTime

    assert job.instance_hours == 0.0
    assert job.cost_quality == "unavailable"
    assert "não chegou a iniciar" in job.cost_unavailable_reason
    assert job.modeled_cost is None


def test_an_unknown_instance_type_names_the_missing_rate():
    job = _job(
        "tipo_novo",
        ProcessingStartTime=INICIO,
        ProcessingResources={
            "ClusterConfig": {"InstanceType": "ml.zz.42xlarge", "InstanceCount": 1}
        },
    )

    assert job.cost_quality == "unavailable"
    assert "ml.zz.42xlarge" in job.cost_unavailable_reason


def test_the_reason_reaches_the_rule():
    """Três causas diferentes viravam a mesma frase para quem lê o relatório."""
    from julius.knowledge.rules.sagemaker.estimation import failed_job_cost

    estimativa = failed_job_cost(_job("nao_iniciou"), DEFAULT_CONFIG)

    assert estimativa.saving_quality == "unavailable"
    assert any("não chegou a iniciar" in item for item in estimativa.assumptions)


def test_a_job_without_cost_base_makes_the_redistribution_visible():
    """Job fora do denominador infla os demais; o rateio precisa dizer isso.

    A redistribuição é inevitável sem base de rateio. O que não pode é ela
    acontecer em silêncio, com os jobs restantes parecendo mais caros do que são.
    """
    rodou = _job("rodou", ProcessingStartTime=INICIO)
    sem_base = _job("nao_iniciou")
    conta = Account(account_id="123456789012", sagemaker_jobs=[rodou, sem_base])
    cobertura = SageMakerCostCoverage(
        buckets={"processing": 100.0}, window_days=30
    )

    resultado = allocate_costs(conta, cobertura, {"processing"})

    # O que rodou recebeu o bucket inteiro, e a cobertura explica por quê.
    assert rodou.allocated_cost == 100.0
    assert sem_base.allocated_cost is None
    assert any("sem base de rateio" in gap for gap in resultado.gaps)
