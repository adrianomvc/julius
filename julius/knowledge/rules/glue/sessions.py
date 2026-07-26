"""Detector de Glue Interactive Session ociosa (regra 3)."""

from __future__ import annotations

from julius.collection.models import Account
from julius.config import Config
from julius.findings.build import build
from julius.findings.opportunity import Estimation, Opportunity
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
        if s.dpu > 5 and s.status == "READY":
            out.append(_capacity_review(account, s, config, scan_id))
        if not (idle_high and actually_idle):
            continue
        est = sess_est.idle_saving(s, config)
        target_dpu = max(th.session_min_dpu, min(s.dpu, th.session_min_dpu))
        out.append(
            build(
                account=account.account_id,
                asset_type="glue_session",
                asset_name=s.session_id,
                rule_id="GLUE-IS-IDLE",
                rule_version="1.0.0",
                difficulty=1,
                estimation=est,
                finding="Sessão interativa ociosa",
                why=(
                    f"Sessões ficam READY ociosas ~{s.idle_hours_per_day:.1f}h/dia; "
                    f"idle_timeout={s.idle_timeout_min} min (default) e DPU={s.dpu}."
                ),
                recommended_action="Reduzir idle_timeout e a DPU da sessão",
                how_to_apply=(
                    f"Ajustar %idle_timeout para 60 min (era {s.idle_timeout_min}) "
                    f"e revisar DPU {s.dpu}→{target_dpu}."
                ),
                how_to_validate="Medir tempo READY ocioso e DPU-h por sessão na próxima semana.",
                evidence=[
                    f"idle_timeout={s.idle_timeout_min} min (default)",
                    f"média {s.idle_hours_per_day:.1f}h READY ocioso/dia",
                    f"DPU={s.dpu} (mín. útil {th.session_min_dpu})",
                ],
                risks=["perder estado da sessão em uso ativo"],
                doc_links=[_DOC],
                data_sources=["Glue GetSession", "CloudWatch"],
                observed_runs=max(s.observed_runs, s.active_days_per_month),
                coverage_days=s.coverage_days,
                has_optional_metrics=s.idle_hours_per_day > 0,
                owner_tag=s.owner_tag,
                config=config,
                scan_id=scan_id,
            )
        )
    return out


def _capacity_review(account, session, config: Config, scan_id: str) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="glue_session",
        asset_name=session.session_id,
        rule_id="GLUE-IS-CAPACITY-REVIEW",
        rule_version="1.0.0",
        difficulty=2,
        estimation=Estimation(
            method="glue_session_capacity_review_v1",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=["capacidade não é reduzida sem Spark UI/logs de executores"],
            pricing_region=config.pricing.region,
            estimation_version=config.pricing.version,
        ),
        finding="Sessão com capacidade acima do default",
        why=f"Sessão READY configurada com {session.dpu:.1f} DPU.",
        recommended_action="Coletar atividade e revisar MaxCapacity/workers",
        how_to_apply="Analisar statements, logs e Spark UI antes de alterar a sessão.",
        how_to_validate="Comparar DPU-h e tempo de resposta em sessão controlada.",
        evidence=[
            f"DPU={session.dpu:.1f}",
            f"worker_type={session.worker_type or 'não informado'}",
            f"number_of_workers={session.number_of_workers or 'não informado'}",
        ],
        risks=["capacidade menor pode degradar desenvolvimento interativo"],
        doc_links=[_DOC],
        data_sources=["Glue ListSessions"],
        observed_runs=max(session.observed_runs, 1),
        coverage_days=session.coverage_days,
        has_optional_metrics=False,
        owner_tag=session.owner_tag,
        config=config,
        scan_id=scan_id,
        blocked=True,
    )
