"""Detectores de Glue Interactive Session: ociosa, superdimensionada, e a que
deveria ser um job."""

from __future__ import annotations

from julius.collection.models import Account
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal
from julius.knowledge.rules.glue import sessions_estimation as sess_est
from julius.knowledge.signal_potential import potential

_DOC = "https://docs.aws.amazon.com/glue/latest/dg/interactive-sessions.html"
_DOC_JOB = "https://docs.aws.amazon.com/glue/latest/dg/author-job.html"

#: Fração do custo da sessão que a migração para job pode devolver.
#:
#: Uma sessão cobra o tempo READY inteiro — inclusive o intervalo entre um
#: statement e o seguinte, que é tempo de gente pensando. Um job cobra a
#: execução. A fração é premissa, e é por isso que isto é faixa e não economia.
_FRACAO_MIGRACAO = 0.35


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for s in account.interactive_sessions:
        idle_high = s.idle_timeout_min > th.session_idle_timeout_high_min
        actually_idle = s.idle_hours_per_day > 1.0 and (
            s.activity_evidence or s.idle_hours_per_day > 0
        )
        if not (idle_high and actually_idle):
            continue
        est = sess_est.idle_saving(s, config)
        out.append(
            build(
                Finding(
                    asset_type="glue_session",
                    asset_name=s.session_id,
                    rule_id="GLUE-IS-IDLE-TIMEOUT",
                    rule_version="2.0.0",
                    title="Sessão interativa ociosa",
                    why=(
                        f"Sessões ficam READY ociosas ~{s.idle_hours_per_day:.1f}h/dia; "
                        f"idle_timeout={s.idle_timeout_min} min."
                    ),
                ),
                Recommendation(
                    difficulty=1,
                    action="Reduzir somente o idle_timeout da sessão",
                    how_to_apply=(
                        f"Ajustar %idle_timeout para 60 min "
                        f"(era {s.idle_timeout_min})."
                    ),
                    how_to_validate="Medir tempo READY ocioso e DPU-h por sessão na próxima semana.",
                    risks=["perder estado da sessão em uso ativo"],
                    docs=[_DOC],
                ),
                Evidence(
                    items=[
                        f"idle_timeout={s.idle_timeout_min} min (default)",
                        f"média {s.idle_hours_per_day:.1f}h READY ocioso/dia",
                        f"DPU={s.dpu} mantida no contrafactual",
                    ],
                    sources=["Glue GetSession", "CloudWatch"],
                    observed_runs=max(s.observed_runs, s.active_days_per_month),
                    coverage_days=s.coverage_days,
                    has_optional_metrics=s.idle_hours_per_day > 0,
                    owner_tag=s.owner_tag,
                ),
                est,
                RuleContext(
                    account=account.account_id,
                    config=config,
                    scan_id=scan_id,
                ),
            )
        )
    return out


def _to_job_signal(session, config: Config) -> Signal:
    """Sessão que roda como job, pagando preço de sessão.

    **Por que sinal e não achado.** A parte medível do desperdício de uma sessão
    — o tempo READY ocioso — já é reivindicada por `GLUE-IS-IDLE-TIMEOUT`, com
    cifra e contrafactual. Migrar para job cobre esse mesmo dinheiro e mais um
    pouco: as duas ações são **alternativas**, não complementares, e emitir as
    duas com economia faria o portfólio somar a mesma sessão duas vezes.

    O que sobra para esta regra é a pergunta que nenhuma métrica responde: o que
    esta sessão executa é trabalho batch, que um job faria igual, ou é
    exploração que exige o ciclo interativo? Isso se responde lendo os
    statements, e é exatamente o que a análise contextual faz.

    O gatilho é a **recorrência**, não o custo. Sessão cara e única é exploração
    legítima; sessão que reabre todo dia com o mesmo trabalho é um job que
    ninguém escreveu.
    """
    # Só o custo alocado. `estimated_dpu_hours_window` é `float = 0.0`, e aí zero
    # significa ao mesmo tempo "medi e deu zero" e "não medi" — `potential`
    # devolve `None` sem baseline, que é a resposta certa: sem custo atribuído
    # não há ordem de grandeza a informar.
    return Signal(
        kind="config",
        rule_id="GLUE-IS-TO-JOB",
        asset_type="glue_session",
        asset_name=session.session_id,
        observation=(
            f"Sessão com {session.observed_runs} abertura(s) observada(s) na "
            f"janela e {len(session.statement_ids)} statement(s) executado(s)."
        ),
        question=(
            "O que esta sessão executa é trabalho batch, que um job faria igual "
            "e cobrando só a execução, ou é exploração que precisa do ciclo "
            "interativo? Se for batch, migrar dispensa o ajuste de idle_timeout."
        ),
        missing_evidence=[
            "statements executados, para saber se o trabalho é repetível",
            "duração de uma execução equivalente como job",
        ],
        doc_links=[_DOC_JOB],
        potential_range=potential(
            session.allocated_cost,
            fraction=_FRACAO_MIGRACAO,
            basis="custo da sessão na janela",
            caveat=(
                "um job cobra a execução e a sessão cobra o tempo READY inteiro; "
                "a fração é premissa. Sobrepõe-se ao ajuste de idle_timeout na "
                "mesma sessão — são caminhos alternativos, e o teto por ativo "
                "impede que os dois somem além do que a sessão custa"
            ),
        ),
    )


def signals(account: Account, config: Config) -> list[Signal]:
    """Capacidade acima do default não é desperdício comprovado.

    A regra anterior disparava em `dpu > 5 and status == "READY"` — nenhuma
    medida de uso entrava na conta, só a distância de um default. Quem sabe se
    a capacidade é excessiva é quem vê o que a sessão executa.
    """
    # A porta é `observed_runs`, e não `active_days_per_month`: o segundo tem
    # default 22 — "dias úteis do mês" —, então uma sessão que ninguém mediu
    # passaria por recorrente. Contador que vale zero quando não foi medido é o
    # único que silencia sozinho.
    recorrentes = [
        _to_job_signal(session, config)
        for session in account.interactive_sessions
        if session.observed_runs >= config.thresholds.recurring_runs_min
        and session.statement_ids
    ]
    return recorrentes + [
        Signal(
            kind="config",
            rule_id="GLUE-IS-CAPACITY-REVIEW",
            asset_type="glue_session",
            asset_name=session.session_id,
            observation=(
                f"Sessão READY com {session.dpu:.1f} DPU "
                f"(worker_type={session.worker_type or 'não informado'})."
            ),
            question=(
                "O trabalho executado nesta sessão justifica a capacidade "
                "configurada, ou ela foi dimensionada por hábito?"
            ),
            missing_evidence=[
                "statements executados na sessão",
                "uso de executores pela Spark UI",
            ],
            doc_links=[_DOC],
        )
        for session in account.interactive_sessions
        if session.dpu > 5 and session.status == "READY"
    ]
