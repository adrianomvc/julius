"""Testes do pipeline determinístico e dos 18 detectores do MVP 1B."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from julius.collection.normalizers import load_account
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules import run_all
from julius.pipeline import analyze
from julius.reporting import renderer

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "consumer-avi.json"


@pytest.fixture(scope="module")
def analysis():
    return analyze(SAMPLE)


def test_detectors_cover_expected_rules():
    # Detecção crua (antes do agrupamento) cobre os ~10 detectores do MVP 1B.
    account = load_account(SAMPLE)
    rule_ids = {o.rule_id for o in run_all(account, DEFAULT_CONFIG, "scan-test")}
    expected = {
        "GLUE-AUTOSCALING",
        "GLUE-OVERPROVISIONED",
        "GLUE-VERSION-OLD",
        "GLUE-BOOKMARK-OFF",
        "GLUE-IS-IDLE",
        "ATHENA-NO-PARTITION-FILTER",
        "ATHENA-EXCESSIVE-SCAN",
        "ATHENA-RESULT-REUSE",
        "GLUE-FAILING-JOB",
        "GLUE-TIMEOUT-EXCESSIVE",
        "GLUE-WORKER-TYPE-OVERSIZED",
        "GLUE-UNATTRIBUTED-COST",
        "DATA-UNUSED-OUTPUT",
        "DATA-LOW-USE-SINGLE-CONSUMER",
        "SFN-STANDARD-TO-EXPRESS",
        "SFN-POLLING-LOOP",
        "SM-APP-IDLE",
        "SM-ENDPOINT-UNUSED",
    }
    assert expected <= rule_ids


def test_stepfunctions_and_sagemaker(analysis):
    ids = {o.rule_id for o in analysis.opportunities}
    # Após agrupamento, ao menos SageMaker (assets distintos) permanece visível.
    assert {"SM-APP-IDLE", "SM-ENDPOINT-UNUSED"} <= ids
    sm_app = next(o for o in analysis.opportunities if o.rule_id == "SM-APP-IDLE")
    assert sm_app.estimated_gain.monthly_expected > 0
    assert sm_app.asset_type == "sagemaker_app"


def test_unused_output_detected(analysis):
    # Base recorrente sem toques vira oportunidade com o custo do job escritor.
    unused = [o for o in analysis.opportunities if o.rule_id == "DATA-UNUSED-OUTPUT"]
    assert unused, "esperava um DATA-UNUSED-OUTPUT"
    o = unused[0]
    assert o.asset_type == "table"
    assert o.estimated_gain.monthly_expected > 0
    assert "toque" in o.finding.lower() or "sem uso" in o.finding.lower()


def test_failing_job_wastes_dpu(analysis):
    # Job que falha e cobra DPU-hora vira oportunidade com economia recuperável.
    failing = [o for o in analysis.opportunities if o.rule_id == "GLUE-FAILING-JOB"]
    assert failing, "esperava um GLUE-FAILING-JOB"
    o = failing[0]
    assert o.estimated_gain.monthly_expected > 0
    assert "DPU-hora" in o.finding or "falha" in o.finding.lower()


def test_root_cause_grouping(analysis):
    # processa_interacoes aciona várias regras → um único item com relacionados.
    procs = [o for o in analysis.opportunities if o.asset_name == "processa_interacoes"]
    assert len(procs) == 1
    assert "achados relacionados" in procs[0].finding
    assert any("Achados relacionados no mesmo ativo" in e for e in procs[0].evidence)


def test_savings_are_positive(analysis):
    for o in analysis.opportunities:
        if not o.estimated_gain.is_strategic:
            if o.estimation.estimated_saving == 0:
                assert o.blocked is True
                assert o.bucket == "investigar_primeiro"
            else:
                assert o.estimation.estimated_saving > 0
            assert o.estimated_gain.realizable_year >= 0


def test_quick_wins_surface(analysis):
    do_now = [o for o in analysis.opportunities if o.bucket == "fazer_agora"]
    assert do_now, "esperava ao menos uma oportunidade em 'fazer_agora'"
    assert all(o.difficulty_score <= 2 for o in do_now)


def test_actionable_gate_blocks_missing_owner(analysis):
    # dashboard_adhoc não tem owner_tag → não acionável → investigar_primeiro.
    dashboard = next(o for o in analysis.opportunities if o.asset_name == "dashboard_adhoc")
    assert dashboard.actionable is False
    assert dashboard.bucket == "investigar_primeiro"
    assert dashboard.next_action == "identificar responsável"


def test_pareto_two_cuts(analysis):
    vm = analysis.vm
    assert 0 < vm.pareto_pct <= 100
    assert vm.pareto_count >= 1
    assert vm.executable_count >= 1


def test_recommendation_is_deterministic(analysis):
    assert "Começar por" in analysis.vm.recommendation


def test_producer_recommendations(analysis):
    # 4 candidatos do dataset, com recomendações determinísticas por quadrante.
    recs = {p.name: p.rec_label for p in analysis.vm.producers}
    assert recs["produto_recomendacoes"] == "Migrar"        # 92/70 alta+alta
    assert recs["fluxo_scoring_leads"] == "Preparar"        # 80/35 alta cand, baixa prontidão
    assert recs["pipeline_relatorios"] == "Monitorar"       # 45/62 baixa cand, alta prontidão
    assert recs["export_adhoc_planilha"] == "Não priorizar"  # 28/24 baixa+baixa


def test_previous_results_precision(analysis):
    prev = {r.title: r for r in analysis.vm.previous_results}
    # A conversão de moeda preserva a razão previsto × realizado.
    assert prev["Redução de workers"].precision == 87
    assert prev["Redução de workers"].predicted_fmt.startswith("US$")


def test_report_json_calls_billing_cost_mtd_by_its_real_period(analysis):
    payload = json.loads(renderer.render_json(analysis.vm, analysis.opportunities))
    assert payload["summary"]["billing_cost_mtd"].startswith("US$")
    assert "total_cost_monthly" not in payload["summary"]


def test_html_has_no_unrendered_template(analysis):
    html = renderer.render_html(analysis.vm)
    assert "{{" not in html and "{%" not in html
    assert "consumer-avi" in html
    assert "Candidatos à Producer" in html
    assert "Resultado das recomendações anteriores" in html


def test_config_signals_leave_the_backlog_and_reach_the_analysis_package(analysis):
    """O que depende de intenção não disputa vaga no ranking.

    Versão abaixo da preferencial e frequência alta são fatos, mas migrar
    runtime depende de bibliotecas que só o script revela, e rodar 720 vezes
    pode ser exatamente o certo para a fonte. Viram hipótese, não achado.
    """
    reclassified = {
        "GLUE-VERSION-REVIEW",
        "GLUE-FREQUENCY-REVIEW",
        "GLUE-IS-CAPACITY-REVIEW",
    }
    backlog_rules = {o.rule_id for o in analysis.opportunities}

    assert not reclassified & backlog_rules
    for signal in analysis.signals:
        assert signal.kind in {"code", "config"}
        assert signal.observation and signal.question

    # O desperdício aritmético continua sendo achado determinístico: abaixo de
    # 2.0 o faturamento em blocos de 10 min é tabelado, não é julgamento.
    detected = {
        o.rule_id for o in run_all(load_account(SAMPLE), DEFAULT_CONFIG, "scan-split")
    }
    assert "GLUE-VERSION-OLD" in detected
    assert not reclassified & detected


def test_inventory_integrity_is_reported_outside_the_portfolio(analysis):
    """Divergência de cron e cobrança não atribuída medem coleta, não economia."""
    integrity = [
        o for o in analysis.opportunities if o.category == "inventory_integrity"
    ]
    assert integrity, "esperava ao menos um achado de integridade"
    assert {"GLUE-UNATTRIBUTED-COST"} <= {o.rule_id for o in integrity}

    ranked_ids = {o.id for o in analysis.vm.table}
    assert not ranked_ids & {o.opportunity_id for o in integrity}
    assert {row["rule_id"] for row in analysis.vm.inventory_integrity} == {
        o.rule_id for o in integrity
    }


def test_athena_layout_findings_carry_no_financial_claim(analysis):
    """Config é fato; quanto ela rende depende do padrão de acesso."""
    layout = [
        o
        for o in analysis.opportunities
        if o.rule_id
        in {
            "ATHENA-PARTITION-PROJECTION",
            "ATHENA-UNCOMPRESSED-ROW-FORMAT",
            "ATHENA-COLUMNAR-COMPRESSION",
        }
    ]
    for opportunity in layout:
        assert opportunity.estimated_gain.is_strategic is True
