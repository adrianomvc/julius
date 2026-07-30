"""Detector de Glue Interactive Session ociosa (regra 3)."""

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

_DOC = "https://docs.aws.amazon.com/glue/latest/dg/interactive-sessions.html"


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


def signals(account: Account, config: Config) -> list[Signal]:
    """Capacidade acima do default não é desperdício comprovado.

    A regra anterior disparava em `dpu > 5 and status == "READY"` — nenhuma
    medida de uso entrava na conta, só a distância de um default. Quem sabe se
    a capacidade é excessiva é quem vê o que a sessão executa.
    """
    return [
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
