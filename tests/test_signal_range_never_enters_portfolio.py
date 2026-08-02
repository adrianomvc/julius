"""A faixa de um sinal informa prioridade e nunca vira dinheiro comprometido.

A disciplina do `Signal` sempre foi: sem economia, sem posição no ranking, sem
backlog. A faixa de ordem de grandeza foi acrescentada depois, e ela é
exatamente o tipo de coisa que corrói essa fronteira sem ninguém decidir que
deveria — um número ao lado de um achado começa a parecer o número do achado.

Este arquivo é o análogo de `test_inferred_never_backs_a_figure.py`, e tem a
mesma estrutura: a invariante, mais a contraprova de que ela não está sendo
satisfeita por bloquear tudo. Uma trava que também impede a `Opportunity` de
produzir cifra passaria neste arquivo e teria quebrado o produto.
"""

from __future__ import annotations

from dataclasses import fields

from julius.collection.models import Account, GlueJob, StateMachine
from julius.config import DEFAULT_CONFIG
from julius.findings.signal import PotentialRange, Signal
from julius.knowledge.rules.stepfunctions import rules as sfn_rules
from julius.knowledge.signal_potential import potential
from julius.reporting.formatters import money
from julius.pipeline import analyze_account

SCRIPT_HEURISTICO = """
from awsglue.context import GlueContext

df = spark.read.parquet("s3://bucket/raw/").filter("d >= '2026-01-01'")
joined = df.join(df, "id")
grouped = joined.groupBy("id")
cached = grouped.cache()
df.count()
df.show()
df.collect()
"""


def _job() -> GlueJob:
    return GlueJob(
        name="job-code",
        glue_version="5.1",
        command_type="glueetl",
        worker_type="G.1X",
        number_of_workers=10,
        runs_in_window=10,
        observed_runs=10,
        coverage_days=30,
        dpu_seconds_window=360000,
        has_spill_evidence=True,
        shuffle_write_bytes=8 * 1024**3,
        spark_event_log_evidence_complete=True,
    )


def _artefato(content: str):
    from julius.collection.artifacts import CodeArtifact

    return CodeArtifact(
        asset_name="job-code",
        source="s3://scripts/job-code.py",
        content=content,
        sha256="b" * 64,
        kind="glue_script",
    )


def test_a_signal_range_is_never_committed_money():
    """Nem no total identificado, nem no realizável, nem no ranking."""
    account = Account(account_id="123456789012", glue_jobs=[_job()])

    analise = analyze_account(
        account,
        DEFAULT_CONFIG,
        scan_id="scan-faixa",
        code_artifacts=[_artefato(SCRIPT_HEURISTICO)],
    )

    com_faixa = [s for s in analise.signals if s.potential_range is not None]
    assert com_faixa, "sem faixa nenhuma, o teste não prova nada"

    total_das_faixas = sum(s.potential_range.expected for s in com_faixa)
    assert total_das_faixas > 0, "faixa zerada não prova exclusão de nada"

    # O ranking e os totais do relatório saem de `opportunities`. Nenhum
    # `rule_id` de sinal pode aparecer entre eles — se aparecesse, a faixa teria
    # entrado no portfólio por uma porta que ninguém abriu de propósito.
    regras_de_sinal = {s.rule_id for s in analise.signals}
    assert not regras_de_sinal & {o.rule_id for o in analise.opportunities}
    # O `id` de uma linha do relatório carrega a rule_id do achado.
    assert not any(
        regra in item.id for regra in regras_de_sinal for item in analise.vm.table
    )

    # E o número de manchete do relatório é exatamente a soma do que tem cifra
    # própria — somar as faixas daria outro valor, e é esse outro valor que
    # nunca pode aparecer ali.
    esperado = sum(
        o.portfolio_gain.monthly_expected
        for o in analise.opportunities
        if o.include_in_portfolio
    )
    assert analise.vm.identified_fmt == money(esperado, account.currency)
    assert analise.vm.identified_fmt != money(
        esperado + total_das_faixas, account.currency
    )


def test_the_measured_path_still_produces_a_figure():
    """A contraprova: a trava não pode ser satisfeita bloqueando tudo."""
    account = Account(account_id="123456789012", glue_jobs=[_job()])

    analise = analyze_account(
        account,
        DEFAULT_CONFIG,
        scan_id="scan-cifra",
        code_artifacts=[_artefato(SCRIPT_HEURISTICO)],
    )

    assert any(o.estimated_gain.monthly_expected > 0 for o in analise.opportunities)


def test_a_signal_has_no_field_that_a_portfolio_would_sum():
    """A forma do `Signal` é parte da trava, não só o que o pipeline faz.

    Um campo chamado `estimated_saving` num sinal seria somado por alguém, mais
    cedo ou mais tarde, sem nenhuma decisão a respeito.
    """
    nomes = {campo.name for campo in fields(Signal)}

    assert not nomes & {
        "estimated_saving",
        "estimated_gain",
        "monthly_expected",
        "realizable_year",
        "annual_potential",
    }
    assert "potential_range" in nomes


def test_the_range_never_claims_to_be_measured():
    """`quality` fixa em `potential` é o que separa faixa de cifra."""
    faixa = potential(
        1000.0, fraction=0.2, basis="custo do ativo", caveat="premissa"
    )

    assert isinstance(faixa, PotentialRange)
    assert faixa.quality == "potential"
    assert faixa.low < faixa.expected < faixa.high == 200.0


def test_no_baseline_produces_no_range_instead_of_zero():
    """Zero se leria como "não há o que ganhar", que é outra afirmação."""
    assert potential(None, fraction=0.2, basis="x", caveat="y") is None
    assert potential(0.0, fraction=0.2, basis="x", caveat="y") is None


def test_a_step_functions_signal_ranks_by_measured_transitions():
    """A base é medida mesmo quando a fração não é: transições × tarifa."""
    machine = StateMachine(
        name="orquestra",
        type="STANDARD",
        executions_per_month=45000,
        avg_duration_sec=90.0,
        avg_state_transitions=12,
        coverage_days=30,
        asl_patterns={"catch_swallow": ["Trabalha"]},
    )
    account = Account(account_id="123456789012", state_machines=[machine])

    sinal = next(
        s
        for s in sfn_rules.signals(account, DEFAULT_CONFIG)
        if s.rule_id == "SFN-ASL-CATCH-SWALLOW"
    )

    assert sinal.potential_range is not None
    assert "transições" in sinal.potential_range.basis
    assert "não medida" in sinal.potential_range.caveat
