"""Pipeline determinístico do MVP 2: inventário → grafo → detecção → contexto →
agrupamento → persistência → KPIs → view model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from julius.audit import build_manifest, new_scan_id
from julius.config import Config, DEFAULT_CONFIG
from julius.governance import compute_candidates
from julius.graph import ProcessGraph, build_process_graph, enrich_opportunities
from julius.ingest import load_account
from julius.inventory.model import Account
from julius.metrics import ProductKPIs, compute_kpis
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors import run_all
from julius.opportunities.grouping import group_by_asset
from julius.opportunities.prioritizer import tiebreak_key
from julius.report.view_models import ReportViewModel, build as build_vm
from julius.state import BacklogStore, HistoryStore


@dataclass
class Analysis:
    account: Account
    opportunities: list[Opportunity]
    vm: ReportViewModel
    kpis: ProductKPIs
    scan_id: str
    graph: ProcessGraph


def analyze(
    input_path: str | Path,
    config: Config = DEFAULT_CONFIG,
    *,
    store: BacklogStore | None = None,
    history: HistoryStore | None = None,
    labels: dict[str, bool] | None = None,
    today: date | None = None,
) -> Analysis:
    return analyze_account(
        load_account(input_path),
        config,
        store=store,
        history=history,
        labels=labels,
        today=today,
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
) -> Analysis:
    scan_id = new_scan_id()
    # Governança: candidatos a Producer calculados quando não fornecidos.
    if not account.producer_candidates:
        account.producer_candidates = compute_candidates(account)
    graph = build_process_graph(account)
    opportunities = run_all(account, config, scan_id)
    enrich_opportunities(account, graph, opportunities)
    # Consolida achados do mesmo ativo numa ação principal (causa raiz).
    opportunities = group_by_asset(opportunities)
    # Ordena por prioridade de execução, com desempate determinístico.
    opportunities.sort(key=lambda o: (o.execution_priority, *tiebreak_key(o)), reverse=True)

    # Persistência: preserva first_seen/status, marca last_seen, detecta desaparecidas.
    if store is not None:
        store.reconcile(opportunities, scan_id, today)

    if labels is None and history is not None:
        labels = history.labels_for(opportunities)
    kpis = compute_kpis(account, opportunities, labels)
    if history is not None:
        history.record_run(
            account,
            opportunities,
            scan_id,
            source=source,
            scanned_on=today,
        )
    manifest = build_manifest(account, config, scan_id, source=source)
    vm = build_vm(account, opportunities, manifest)
    return Analysis(
        account=account,
        opportunities=opportunities,
        vm=vm,
        kpis=kpis,
        scan_id=scan_id,
        graph=graph,
    )
