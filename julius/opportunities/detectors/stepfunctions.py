"""Detectores de Step Functions: Standard→Express e loop de polling."""

from __future__ import annotations

from julius.collection.models import Account, StateMachine
from julius.config import Config
from julius.estimation import stepfunctions as sfn_est
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC_EXPRESS = "https://docs.aws.amazon.com/step-functions/latest/dg/concepts-standard-vs-express.html"
_DOC_SYNC = "https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for sm in account.state_machines:
        if (
            sm.type == "STANDARD"
            and sm.executions_per_month >= th.sfn_express_min_executions
            and 0 < sm.avg_duration_sec <= th.sfn_short_duration_sec
            and sm.idempotent is True
        ):
            out.append(_to_express(account, sm, config, scan_id))

        if sm.type == "STANDARD" and sm.has_polling_loop and sm.poll_extra_transitions > 0:
            out.append(_polling(account, sm, config, scan_id))
    return out


def _to_express(account: Account, sm: StateMachine, config: Config, scan_id: str) -> Opportunity:
    est = sfn_est.standard_to_express_saving(sm, config)
    return build(
        account=account.account_id, asset_type="state_machine", asset_name=sm.name,
        rule_id="SFN-STANDARD-TO-EXPRESS", rule_version="1.0.0", difficulty=3, estimation=est,
        finding="Standard Workflow candidato a Express",
        why=f"{sm.executions_per_month} execuções/mês curtas ({sm.avg_duration_sec:.0f}s) e idempotentes — Express é ~25× mais barato.",
        recommended_action="Avaliar migração de Standard para Express",
        how_to_apply="Recriar a state machine como EXPRESS; validar idempotência e o limite de 5 min.",
        how_to_validate="Comparar custo (transições × execuções) antes × depois.",
        evidence=[
            f"type=STANDARD, {sm.executions_per_month} exec/mês",
            f"{sm.avg_state_transitions} transições/execução",
            f"duração média {sm.avg_duration_sec:.0f}s",
        ],
        risks=["Express é at-least-once", "limite de 5 min por execução"],
        doc_links=[_DOC_EXPRESS], data_sources=["States DescribeStateMachine", "CloudWatch"],
        observed_runs=sm.observed_runs, coverage_days=sm.coverage_days,
        has_optional_metrics=True, owner_tag=sm.owner_tag, config=config, scan_id=scan_id, risk=0.6,
    )


def _polling(account: Account, sm: StateMachine, config: Config, scan_id: str) -> Opportunity:
    est = sfn_est.polling_loop_saving(sm, config)
    return build(
        account=account.account_id, asset_type="state_machine", asset_name=sm.name,
        rule_id="SFN-POLLING-LOOP", rule_version="1.0.0", difficulty=2, estimation=est,
        finding="Loop de polling gera transições extras",
        why=f"Loop Wait→Task→Choice→Wait adiciona ~{sm.poll_extra_transitions} transições/execução — evitável com .sync/callback.",
        recommended_action="Trocar polling por integração .sync ou callback (Task Token)",
        how_to_apply="Usar o padrão .sync do Glue/Athena ou waitForTaskToken em vez do loop de espera.",
        how_to_validate="Comparar nº de transições por execução antes × depois.",
        evidence=[
            "padrão Wait→Task→Choice→Wait detectado",
            f"~{sm.poll_extra_transitions} transições extras/execução",
            f"{sm.executions_per_month} execuções/mês",
        ],
        risks=["exige ajuste no fluxo"],
        doc_links=[_DOC_SYNC], data_sources=["States DescribeStateMachine (ASL)"],
        observed_runs=sm.observed_runs, coverage_days=sm.coverage_days,
        has_optional_metrics=True, owner_tag=sm.owner_tag, config=config, scan_id=scan_id,
    )
