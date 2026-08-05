"""Testes do pipeline determinístico e dos 18 detectores do MVP 1B."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from verified_pricing import verified_config

from julius.collection.normalizers import load_account
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules import run_all
from julius.knowledge.rules.sagemaker import rules as sagemaker_rules
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
        "GLUE-IS-IDLE-TIMEOUT",
        "ATHENA-NO-PARTITION-FILTER",
        "ATHENA-EXCESSIVE-SCAN",
        "ATHENA-RESULT-REUSE",
        "GLUE-FAILING-JOB",
        "GLUE-TIMEOUT-EXCESSIVE",
        "GLUE-WORKER-TYPE-OVERSIZED",
        "GLUE-UNATTRIBUTED-COST",
        "DATA-UNUSED-OUTPUT",
        "DATA-LOW-USE-SINGLE-CONSUMER",
        "SFN-POLLING-LOOP",
        "SM-APP-IDLE",
    }
    assert expected <= rule_ids


def test_stepfunctions_and_sagemaker(analysis):
    ids = {o.rule_id for o in analysis.opportunities}
    # Após agrupamento, o app com 90 dias permanece oportunidade. Endpoint de
    # baixo tráfego, mas não zero, segue como sinal contextual.
    assert "SM-APP-IDLE" in ids
    assert "SM-ENDPOINT-ZERO-TRAFFIC" not in ids
    assert "SM-ENDPOINT-MODE-FIT" in {s.rule_id for s in analysis.signals}
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
    """Causa raiz é a ação de remediação, não o ativo.

    Antes, toda regra sobre um job virava um item só. `processa_interacoes` aciona
    quatro regras — reduzir workers, subir a versão do Glue, ligar bookmark e
    corrigir o timeout —, e são quatro mudanças diferentes, com quatro validações
    diferentes e possivelmente quatro momentos diferentes. Fundidas, três delas
    viravam texto dentro dos riscos da quarta e a economia delas sumia do portfólio.

    O agrupamento continua existindo, e é o que `test_same_family_still_merges`
    cobre: o que se funde é o que descreve a **mesma** correção.
    """
    procs = [o for o in analysis.opportunities if o.asset_name == "processa_interacoes"]
    familias = {o.remediation_family for o in procs}
    assert len(procs) == len(familias) == 4, (
        "cada família de remediação é uma ação própria: "
        f"{sorted((o.rule_id, o.remediation_family) for o in procs)}"
    )
    assert all(o.remediation_family for o in procs), "achado sem família classificada"


def test_same_family_still_merges(analysis):
    """A metade que não mudou: duas regras da mesma correção continuam uma ação.

    `sync_parceiros` aciona `GLUE-FAILING-JOB` e `GLUE-TIMEOUT-EXCESSIVE`, que são
    a mesma família — parar de pagar por execução que falha. Elas se fundem, e a
    cifra do item é a do primário, nunca a soma.
    """
    itens = [
        o
        for o in analysis.opportunities
        if o.asset_name == "sync_parceiros"
        and o.remediation_family == "failure_waste"
    ]
    assert len(itens) == 1
    principal = itens[0]
    assert "achado relacionado" in principal.finding
    assert any(
        "Achados relacionados no mesmo ativo" in item for item in principal.evidence
    )


def test_an_asset_never_claims_more_than_it_costs(analysis):
    """Famílias diferentes somam — mas nunca além do que o ativo custa.

    É o que separa "duas ações reais" de "o mesmo dinheiro contado duas vezes". Sem
    este teto, dois achados sobre a mesma query Athena reivindicariam cada um o
    custo inteiro dela, porque `athena_query` não tem linha em `process_costs` e o
    único limite era o baseline individual.
    """
    from collections import defaultdict

    somado: dict[tuple[str, str], float] = defaultdict(float)
    custo: dict[tuple[str, str], float] = {}
    for o in analysis.opportunities:
        if not (o.include_in_portfolio and o.estimation):
            continue
        chave = (o.asset_type, o.asset_name)
        somado[chave] += o.portfolio_gain.monthly_expected
        custo[chave] = max(custo.get(chave, 0.0), o.estimation.baseline_cost)
    estouros = {k: (v, custo[k]) for k, v in somado.items() if v > custo[k] + 0.01}
    assert not estouros, f"economia acima do custo do ativo: {estouros}"


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
    assert do_now == [], "pricing não verificado não pode liberar quick win financeiro"
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
    assert vm.executable_count == 0


def test_recommendation_is_deterministic(analysis):
    assert "coletar evidência" in analysis.vm.recommendation


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


def test_report_json_calls_billing_cost_period_by_its_real_period(analysis):
    payload = json.loads(renderer.render_json(analysis.vm, analysis.opportunities))
    assert payload["summary"]["billing_cost_period"].startswith("US$")
    assert "total_cost_monthly" not in payload["summary"]


def test_html_has_no_unrendered_template(analysis):
    html = renderer.render_html(analysis.vm)
    assert "{{" not in html and "{%" not in html
    assert "consumer-avi" in html
    # Os candidatos a Producer são triagem de arquitetura, não recomendação de
    # economia: ficaram no JSON quando o HTML virou o documento do analista.
    assert "Candidatos à Producer" not in html
    assert "O que já economizamos antes" in html


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


def test_an_unmeasured_table_is_not_accused_of_being_unused():
    """A fonte de toques é opcional; sem ela toda tabela pareceria órfã."""
    from julius.collection.models import Account, GlueJob, Table
    from julius.knowledge.rules import families_without_evidence, missing_evidence

    account = Account(
        account_id="123456789012",
        glue_jobs=[GlueJob(name="produz", runs_in_window=40, window_days=30)],
        tables=[Table(name="saida", written_by="produz")],
    )

    found = run_all(account, DEFAULT_CONFIG, "scan")
    assert not [o for o in found if o.rule_id.startswith("DATA-")]

    # E o silêncio é explicado, em vez de a seção ficar vazia parecendo boa notícia.
    familias = {f"{f.service}/{f.name}" for f in families_without_evidence(account)}
    assert "cross_service/data_products" in familias
    familia = next(
        f for f in families_without_evidence(account) if f.name == "data_products"
    )
    assert "medição ausente" in missing_evidence(account, familia)


def test_a_measured_zero_still_produces_the_finding():
    """Medir e não achar toque é afirmação legítima — e continua valendo."""
    from julius.collection.models import Account, GlueJob, Table

    account = Account(
        account_id="123456789012",
        glue_jobs=[GlueJob(name="produz", runs_in_window=40, window_days=30)],
        tables=[Table(name="saida", written_by="produz", touches_90d=0)],
    )

    found = run_all(account, DEFAULT_CONFIG, "scan")
    assert "DATA-UNUSED-OUTPUT" in {o.rule_id for o in found}


def test_an_endpoint_without_cloudwatch_is_not_declared_unused():
    """Sem métrica, um endpoint em produção era reportado com o custo 24/7."""
    from julius.collection.models import Account, SageMakerEndpoint

    account = Account(
        account_id="123456789012",
        sagemaker_endpoints=[
            SageMakerEndpoint(
                name="modelo-em-producao",
                instance_type="ml.m5.xlarge",
                instance_count=3,
                coverage_days=30,
            )
        ],
    )

    assert not run_all(account, DEFAULT_CONFIG, "scan")

    account.sagemaker_endpoints[0].invocations_per_month = 4
    assert not run_all(account, DEFAULT_CONFIG, "scan")
    assert "SM-ENDPOINT-MODE-FIT" in {
        signal.rule_id
        for signal in sagemaker_rules.signals(account, DEFAULT_CONFIG)
    }

    account.sagemaker_endpoints[0].invocations_per_month = 0
    account.sagemaker_endpoints[0].coverage_days = 90
    found = run_all(account, DEFAULT_CONFIG, "scan")
    assert "SM-ENDPOINT-ZERO-TRAFFIC" in {o.rule_id for o in found}


def test_flex_is_no_longer_dead_and_waits_for_the_sla_answer():
    """`time_sensitive` nunca foi escrito por ninguém; a regra nunca disparava."""
    from julius.collection.models import Account, GlueJob
    from julius.knowledge.rules import collect_signals

    job = GlueJob(
        name="batch",
        glue_version="4.0",
        command_type="glueetl",
        execution_class="STANDARD",
        worker_type="G.1X",
        number_of_workers=10,
        runs_in_window=30,
        window_days=30,
        avg_execution_sec=1800,
        dpu_seconds_window=540000,
        observed_runs=30,
        coverage_days=30,
    )
    account = Account(account_id="123456789012", glue_jobs=[job])

    config = verified_config("glue")
    found = {o.rule_id: o for o in run_all(account, config, "scan")}
    flex = found["GLUE-FLEX-CANDIDATE"]
    assert flex.blocked is True, "migrar sem saber o SLA seria recomendar às cegas"
    assert flex.missing_evidence

    sinais = {s.rule_id for s in collect_signals(account, config)}
    assert "GLUE-FLEX-TOLERANCE" in sinais

    # A afirmação explícita libera a recomendação e cala a pergunta.
    job.time_sensitive = False
    flex = {o.rule_id: o for o in run_all(account, config, "scan")}[
        "GLUE-FLEX-CANDIDATE"
    ]
    assert flex.blocked is False
    assert "GLUE-FLEX-TOLERANCE" not in {
        s.rule_id for s in collect_signals(account, config)
    }
