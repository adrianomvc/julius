"""Detectores de Step Functions: Standard→Express e loop de polling."""

from __future__ import annotations

from julius.collection.models import Account, StateMachine
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Opportunity
from julius.findings.recommendation import Recommendation
from julius.knowledge.rules.stepfunctions import estimation as sfn_est

_DOC_EXPRESS = (
    "https://docs.aws.amazon.com/step-functions/latest/dg/concepts-standard-vs-express.html"
)
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
        Finding(
            asset_type="state_machine",
            asset_name=sm.name,
            rule_id="SFN-STANDARD-TO-EXPRESS",
            rule_version="1.0.0",
            title="Standard Workflow candidato a Express",
            why=f"{sm.executions_per_month} execuções/mês curtas ({sm.avg_duration_sec:.0f}s) e idempotentes — Express é ~25× mais barato.",
        ),
        Recommendation(
            difficulty=3,
            action="Avaliar migração de Standard para Express",
            how_to_apply="Recriar a state machine como EXPRESS; validar idempotência e o limite de 5 min.",
            how_to_validate="Comparar custo (transições × execuções) antes × depois.",
            risks=["Express é at-least-once", "limite de 5 min por execução"],
            docs=[_DOC_EXPRESS],
            risk=0.6,
        ),
        Evidence(
            items=[
                f"type=STANDARD, {sm.executions_per_month} exec/mês",
                f"{sm.avg_state_transitions} transições/execução",
                f"duração média {sm.avg_duration_sec:.0f}s",
            ],
            sources=["States DescribeStateMachine", "CloudWatch"],
            observed_runs=sm.observed_runs,
            coverage_days=sm.coverage_days,
            has_optional_metrics=True,
            owner_tag=sm.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _polling(account: Account, sm: StateMachine, config: Config, scan_id: str) -> Opportunity:
    est = sfn_est.polling_loop_saving(sm, config)
    return build(
        Finding(
            asset_type="state_machine",
            asset_name=sm.name,
            rule_id="SFN-POLLING-LOOP",
            rule_version="1.0.0",
            title="Loop de polling gera transições extras",
            why=f"Loop Wait→Task→Choice→Wait adiciona ~{sm.poll_extra_transitions} transições/execução — evitável com .sync/callback.",
        ),
        Recommendation(
            difficulty=2,
            action="Trocar polling por integração .sync ou callback (Task Token)",
            how_to_apply="Usar o padrão .sync do Glue/Athena ou waitForTaskToken em vez do loop de espera.",
            how_to_validate="Comparar nº de transições por execução antes × depois.",
            risks=["exige ajuste no fluxo"],
            docs=[_DOC_SYNC],
        ),
        Evidence(
            items=[
                "padrão Wait→Task→Choice→Wait detectado",
                f"~{sm.poll_extra_transitions} transições extras/execução",
                f"{sm.executions_per_month} execuções/mês",
            ],
            sources=["States DescribeStateMachine (ASL)"],
            observed_runs=sm.observed_runs,
            coverage_days=sm.coverage_days,
            has_optional_metrics=True,
            owner_tag=sm.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )
