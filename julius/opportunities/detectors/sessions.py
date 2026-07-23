"""Detector de Glue Interactive Session ociosa (regra 3)."""

from __future__ import annotations

from julius.config import Config
from julius.estimation import interactive_sessions as sess_est
from julius.inventory.model import Account
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC = "https://docs.aws.amazon.com/glue/latest/dg/interactive-sessions.html"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for s in account.interactive_sessions:
        idle_high = s.idle_timeout_min > th.session_idle_timeout_high_min
        actually_idle = s.idle_hours_per_day > 1.0
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
