"""Processo da plataforma não vira recomendação — e continua no rateio.

Algumas aplicações chegam prontas na conta Consumer: monitoria, aquecimento de
dado. O time dono não altera a infraestrutura delas nem dá manutenção, e uma
recomendação que ninguém pode executar não é economia — é ruído disputando
posição no ranking com o que dá para fazer.

O ponto delicado é *onde* excluir. Filtrar na coleta parece mais econômico e
quebra a atribuição de custo de todos os outros jobs: `allocate_costs` rateia a
fatura real proporcionalmente à DPU-hora de cada um, então tirar um job do
inventário não tira o consumo dele da fatura — espalha a parte dele sobre os
demais. É esse erro que este arquivo impede de voltar.
"""

from __future__ import annotations

import pytest

from julius.collection.models import Account, GlueJob, StateMachine
from julius.config import DEFAULT_CONFIG
from julius.knowledge.managed_processes import is_managed
from julius.state.audit import build_manifest

_MONITORIA = "analytics-gluejob-mdp-custom-metrics"


# --------------------------------------------------------------------------
# Quem é da plataforma
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nome",
    [
        _MONITORIA,
        "ANALYTICS-GLUEJOB-MDP-CUSTOM-METRICS",
        # O token no fim, no começo e no meio: a posição não decide.
        "consumer-avi-analytics-data-warmer-glue",
        "analytics-data-warmer-glue-processa",
        "analytics-data-warmer-glue-v2",
        "qualquer-prefixo-analytics-data-warm-sfn",
        "analytics-data-warm-sfn-orquestrador",
    ],
)
def test_platform_processes_are_recognized(nome):
    assert is_managed(nome) is True


@pytest.mark.parametrize(
    "nome",
    [
        "agrega_vendas",
        "",
        # O prefixo `analytics-` sozinho não é critério: ele é a convenção de
        # nomenclatura do domínio, e os jobs da própria conta também o usam.
        # Ignorar por prefixo apagaria justamente onde está a economia.
        "analytics-vendas-diario",
        "analytics-etl-clientes",
        # Parecido não é igual: a monitoria é nome exato, não token.
        "analytics-gluejob-mdp-custom-metrics-v2",
    ],
)
def test_the_accounts_own_processes_are_not(nome):
    assert is_managed(nome) is False


# --------------------------------------------------------------------------
# Eles ficam no inventário, e é isso que mantém o custo dos outros honesto
# --------------------------------------------------------------------------


def test_a_platform_process_stays_in_the_inventory():
    """Tirá-lo daqui espalharia a fatura dele sobre os jobs que sobram."""
    from julius.collection.collectors.glue.cost import allocate_costs
    from julius.collection.models import GlueCostCoverage

    conta = Account(
        account_id="1",
        glue_jobs=[
            GlueJob(name=_MONITORIA, dpu_seconds_window=3600 * 3),
            GlueJob(name="agrega_vendas", dpu_seconds_window=3600),
        ],
    )
    coverage = GlueCostCoverage(buckets={"etl_job": 100.0})

    allocate_costs(conta, coverage, DEFAULT_CONFIG, allocatable_buckets={"etl_job"})

    por_nome = {job.name: job.allocated_cost for job in conta.glue_jobs}
    # 3:1 de DPU-hora → 75/25. Sem o processo da plataforma no inventário, o
    # job da conta receberia os 100 inteiros e pareceria 4x mais caro.
    assert por_nome["agrega_vendas"] == pytest.approx(25.0)
    assert por_nome[_MONITORIA] == pytest.approx(75.0)


# --------------------------------------------------------------------------
# E não viram recomendação
# --------------------------------------------------------------------------


def test_no_opportunity_survives_for_a_platform_process():
    """Um job da plataforma com todo defeito que existe não gera nada."""
    from julius.pipeline import analyze_account

    conta = Account(
        account_id="123456789012",
        glue_jobs=[
            GlueJob(
                name=_MONITORIA,
                worker_type="G.2X",
                number_of_workers=20,
                auto_scaling=False,
                job_bookmark=False,
                glue_version="2.0",
                avg_cpu_load=0.05,
                timeout_min=2880,
                avg_execution_sec=300,
                observed_runs=30,
                runs_in_window=30,
                dpu_seconds_window=3600 * 100,
                coverage_days=30,
                window_days=30,
            )
        ],
    )

    analise = analyze_account(conta, DEFAULT_CONFIG, scan_id="scan-teste")

    assert [o.asset_name for o in analise.opportunities if o.asset_name == _MONITORIA] == []


def test_the_same_defects_do_produce_findings_for_the_accounts_own_job():
    """O contraste: sem ele, o teste acima passaria mesmo com a regra quebrada."""
    from julius.pipeline import analyze_account

    conta = Account(
        account_id="123456789012",
        glue_jobs=[
            GlueJob(
                name="agrega_vendas",
                worker_type="G.2X",
                number_of_workers=20,
                auto_scaling=False,
                job_bookmark=False,
                glue_version="2.0",
                avg_cpu_load=0.05,
                timeout_min=2880,
                avg_execution_sec=300,
                observed_runs=30,
                runs_in_window=30,
                dpu_seconds_window=3600 * 100,
                coverage_days=30,
                window_days=30,
            )
        ],
    )

    analise = analyze_account(conta, DEFAULT_CONFIG, scan_id="scan-teste")

    assert [o for o in analise.opportunities if o.asset_name == "agrega_vendas"]


# --------------------------------------------------------------------------
# A exclusão é declarada, não silenciosa
# --------------------------------------------------------------------------


def test_the_manifest_says_which_processes_were_left_out():
    conta = Account(
        account_id="1",
        glue_jobs=[GlueJob(name=_MONITORIA), GlueJob(name="agrega_vendas")],
        state_machines=[StateMachine(name="orquestra-analytics-data-warm-sfn")],
    )

    manifesto = build_manifest(
        conta,
        DEFAULT_CONFIG,
        "scan-1",
        "dataset",
        managed_processes=[
            name
            for name in [job.name for job in conta.glue_jobs]
            + [machine.name for machine in conta.state_machines]
            if is_managed(name)
        ],
    )

    linha = next(item for item in manifesto if item["k"] == "processos da plataforma")
    assert _MONITORIA in linha["v"]
    assert "orquestra-analytics-data-warm-sfn" in linha["v"]
    assert "agrega_vendas" not in linha["v"]


def test_an_account_without_platform_processes_has_no_such_line():
    conta = Account(account_id="1", glue_jobs=[GlueJob(name="agrega_vendas")])

    manifesto = build_manifest(
        conta,
        DEFAULT_CONFIG,
        "scan-1",
        "dataset",
        managed_processes=[
            name
            for name in [job.name for job in conta.glue_jobs]
            + [machine.name for machine in conta.state_machines]
            if is_managed(name)
        ],
    )

    assert all(item["k"] != "processos da plataforma" for item in manifesto)
