"""Pipeline determinístico do MVP 3: inventário → grafo → oportunidades →
ciclo de vida/diff → persistência → KPIs → view model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from julius.audit import build_manifest, new_scan_id
from julius.code_analysis import (
    GlueCodeArtifact,
    load_glue_artifacts,
    summarize_glue_artifact_health,
)
from julius.config import DEFAULT_CONFIG, Config
from julius.estimation.calibration import apply_calibrations
from julius.estimation.process_cost import (
    apply_conservative_caps,
    build_process_costs,
)
from julius.governance import compute_candidates
from julius.graph import ProcessGraph, build_process_graph, enrich_opportunities
from julius.ingest import load_account
from julius.inventory.model import Account, CollectionHealth, PreviousResult
from julius.metrics import ProductKPIs, compute_kpis
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors import glue_code, run_all
from julius.opportunities.grouping import group_by_asset
from julius.opportunities.lifecycle import transition
from julius.opportunities.prioritizer import tiebreak_key
from julius.report.formatters import money
from julius.report.view_models import ReportViewModel
from julius.report.view_models import build as build_vm
from julius.state import (
    BacklogStore,
    BenefitSummary,
    DiffEvent,
    HistoryStore,
    LifecycleLeadTimes,
)
from julius.state.diff import compare
from julius.state.store import Reconciliation


@dataclass
class Analysis:
    account: Account
    opportunities: list[Opportunity]
    vm: ReportViewModel
    kpis: ProductKPIs
    scan_id: str
    graph: ProcessGraph
    events: list[DiffEvent]
    reconciliation: Reconciliation


def analyze(
    input_path: str | Path,
    config: Config = DEFAULT_CONFIG,
    *,
    store: BacklogStore | None = None,
    history: HistoryStore | None = None,
    labels: dict[str, bool] | None = None,
    today: date | None = None,
    scan_id: str | None = None,
    artifacts_manifest: str | Path | None = None,
) -> Analysis:
    account = load_account(input_path)
    code_artifacts = (
        load_glue_artifacts(artifacts_manifest, account.account_id)
        if artifacts_manifest
        else None
    )
    if artifacts_manifest:
        if not account.collection_health:
            account.collection_health.append(
                CollectionHealth(
                    source="Dataset collection telemetry",
                    status="unavailable",
                    error_category="not_reported",
                    impact="a cobertura das fontes do dataset não foi registrada",
                    next_action="gerar o dataset com uma versão atual do julius collect",
                )
            )
        account.collection_health = [
            item
            for item in account.collection_health
            if item.source != "Glue Scripts"
        ]
        account.collection_health.append(
            summarize_glue_artifact_health(
                artifacts_manifest,
                account,
                code_artifacts or [],
            )
        )
    return analyze_account(
        account,
        config,
        store=store,
        history=history,
        labels=labels,
        today=today,
        scan_id=scan_id,
        code_artifacts=code_artifacts,
    )


def analyze_account(
    account: Account,
    config: Config = DEFAULT_CONFIG,
    *,
    store: BacklogStore | None = None,
    history: HistoryStore | None = None,
    labels: dict[str, bool] | None = None,
    today: date | None = None,
    source: str = "dataset exportado",
    scan_id: str | None = None,
    code_artifacts: list[GlueCodeArtifact] | None = None,
) -> Analysis:
    scan_id = scan_id or new_scan_id()
    # Governança: candidatos a Producer calculados quando não fornecidos.
    if not account.producer_candidates:
        account.producer_candidates = compute_candidates(account)
    account.process_costs = build_process_costs(account, config, today=today)
    graph = build_process_graph(account)
    opportunities = run_all(account, config, scan_id)
    if code_artifacts:
        opportunities += glue_code.detect(
            account, code_artifacts, config, scan_id
        )
    enrich_opportunities(account, graph, opportunities)
    # Consolida achados do mesmo ativo numa ação principal (causa raiz).
    opportunities = group_by_asset(opportunities)
    _link_athena_opportunities(account, opportunities)
    # Aplica realização e caps depois do agrupamento para não reservar custo
    # para achados relacionados que não permanecem no portfólio final.
    apply_conservative_caps(account, opportunities, config, today=today)
    if history is not None:
        apply_calibrations(opportunities, history, config)
    # Ordena por prioridade de execução, com desempate determinístico.
    opportunities.sort(key=lambda o: (o.execution_priority, *tiebreak_key(o)), reverse=True)

    previous = history.latest_snapshots(account.account_id) if history is not None else []
    reconciliation = Reconciliation()
    # Persistência: preserva status, reabre por evidência e detecta desaparecidas.
    if store is not None:
        reconciliation = store.reconcile(
            opportunities,
            scan_id,
            today,
            account_id=account.account_id,
        )

    events = compare(previous, opportunities, reconciliation)
    suppressed = set(reconciliation.suppressed)
    opportunities = [
        opportunity
        for opportunity in opportunities
        if opportunity.fingerprint() not in suppressed
    ]
    opportunities.sort(
        key=lambda o: (o.execution_priority, *tiebreak_key(o)), reverse=True
    )

    if labels is None and history is not None:
        labels = history.labels_for(opportunities)
    benefit = (
        history.benefit_summary(account.account_id)
        if history is not None
        else BenefitSummary()
    )
    lead_times = (
        history.lifecycle_lead_times(account.account_id)
        if history is not None
        else LifecycleLeadTimes()
    )
    kpis = compute_kpis(
        account,
        opportunities,
        labels,
        realized_monthly=benefit.realized_monthly,
        detected_to_accepted_days=lead_times.detected_to_accepted_days,
        accepted_to_implemented_days=lead_times.accepted_to_implemented_days,
        implemented_to_validated_days=lead_times.implemented_to_validated_days,
    )
    if history is not None:
        for change in reconciliation.status_changes:
            event = transition(
                fingerprint=change["fingerprint"],
                account=str(change.get("account") or account.account_id),
                opportunity_id=str(change.get("opportunity_id") or ""),
                from_status=change["from_status"],
                to_status=change["to_status"],
                actor="Julius",
                reason=change["reason"],
                automatic=True,
            )
            history.record_lifecycle_event(event)
        history.record_diff_events(scan_id, events)
        history.record_run(
            account,
            opportunities,
            scan_id,
            source=source,
            scanned_on=today,
        )
    if history is not None:
        _merge_validations(account, history.latest_validations(account.account_id))
    manifest = build_manifest(account, config, scan_id, source=source)
    vm = build_vm(account, opportunities, manifest)
    vm.diff_events = [
        {
            "type": event.event_type,
            "asset": event.asset_name,
            "rule_id": event.rule_id,
            "previous": event.previous_value,
            "current": event.current_value,
        }
        for event in events
    ]
    vm.committed_fmt = money(kpis.committed_monthly, account.currency)
    vm.realized_fmt = money(benefit.realized_monthly, account.currency)
    vm.realization_rate_pct = (
        f"{benefit.realization_rate * 100:.0f}%"
        if benefit.realization_rate is not None
        else "—"
    )
    vm.lifecycle_lead_times = [
        {"label": label, "days": value}
        for label, value in (
            ("Detectada → aceita", lead_times.detected_to_accepted_days),
            ("Aceita → implementada", lead_times.accepted_to_implemented_days),
            ("Implementada → validada", lead_times.implemented_to_validated_days),
        )
        if value is not None
    ]
    return Analysis(
        account=account,
        opportunities=opportunities,
        vm=vm,
        kpis=kpis,
        scan_id=scan_id,
        graph=graph,
        events=events,
        reconciliation=reconciliation,
    )


def _merge_validations(account: Account, rows: list[dict]) -> None:
    known = {(item.title, item.date) for item in account.previous_results}
    for row in rows:
        validated_at = row["validated_at"]
        date_text = (
            validated_at.date().isoformat()
            if hasattr(validated_at, "date")
            else str(validated_at)[:10]
        )
        key = (row["opportunity_id"], date_text)
        if key in known:
            continue
        normalized = row.get("normalized_saving")
        unit = (
            f"economia normalizada {money(normalized, account.currency)}/mês"
            if normalized is not None
            else "comparação de custo absoluto"
        )
        account.previous_results.append(
            PreviousResult(
                title=row["opportunity_id"],
                asset=row["rule_id"],
                date=date_text,
                predicted_monthly=float(row["predicted_monthly"]),
                realized_monthly=float(row["realized_monthly"]),
                currency="USD",
                unit=unit,
            )
        )


def _link_athena_opportunities(
    account: Account, opportunities: list[Opportunity]
) -> None:
    """Liga orientação pessoal à ação única do padrão, sem somar economia."""
    refs_by_pattern: dict[str, list[str]] = {}
    for opportunity in opportunities:
        if opportunity.asset_type == "athena_query":
            refs_by_pattern.setdefault(opportunity.asset_name, []).append(
                opportunity.opportunity_id
            )
    refs_by_actor: dict[str, set[str]] = {}
    for query in account.athena_queries:
        query.opportunity_refs = sorted(set(refs_by_pattern.get(query.query_id, [])))
        for actor in query.actors:
            refs_by_actor.setdefault(actor, set()).update(query.opportunity_refs)
    for usage in account.athena_actor_usage:
        usage.opportunity_refs = sorted(refs_by_actor.get(usage.actor, set()))
