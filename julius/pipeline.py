"""Pipeline determinístico do MVP 3: inventário → grafo → oportunidades →
ciclo de vida/diff → persistência → KPIs → view model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from julius.collection.artifacts import (
    GlueCodeArtifact,
    load_glue_artifacts,
    summarize_glue_artifact_health,
)
from julius.collection.collectors import redshift_cost, sagemaker_cost
from julius.collection.collectors.glue import cost as glue_cost
from julius.collection.models import Account, CollectionHealth, PreviousResult
from julius.collection.normalizers import load_account
from julius.config import DEFAULT_CONFIG, Config
from julius.findings.grouping import group_by_asset
from julius.findings.investigation import ContextualEstimate, Investigation
from julius.findings.lifecycle import transition
from julius.findings.opportunity import Opportunity
from julius.findings.signal import Signal
from julius.governance import compute_candidates
from julius.graph import ProcessGraph, build_process_graph, enrich_opportunities
from julius.knowledge.contextual_estimation import evaluate_proposal
from julius.knowledge.managed_processes import is_managed, managed_asset_names
from julius.knowledge.recurrence import (
    consumption_dpu_hours,
    deserves_signal,
    is_recurrent,
    runs_per_month,
)
from julius.knowledge.rules import collect_signals, run_all
from julius.knowledge.rules.glue.code import rules as glue_code
from julius.reporting import ProductKPIs, compute_kpis
from julius.reporting.formatters import money
from julius.reporting.view_models import ReportViewModel
from julius.reporting.view_models import build as build_vm
from julius.scoring.calibration import apply_calibrations
from julius.scoring.priority import ranking_key
from julius.scoring.process_cost import (
    apply_conservative_caps,
    build_process_costs,
)
from julius.state import (
    BacklogStore,
    BenefitSummary,
    DiffEvent,
    HistoryStore,
    LifecycleLeadTimes,
    SignalLedger,
)
from julius.state.audit import build_manifest, new_scan_id
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
    # Hipóteses que nenhuma regra fecha sozinha; não entram no backlog nem no
    # ranking, seguem para a análise contextual.
    signals: list[Signal] = field(default_factory=list)
    investigations: list[Investigation] = field(default_factory=list)


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
    ledger: SignalLedger | None = None,
    cadence: str | None = None,
) -> Analysis:
    account = load_account(input_path)
    if cadence is not None:
        if cadence not in {"weekly", "monthly"}:
            raise ValueError("cadence deve ser weekly ou monthly")
        account.cadence = cadence
        if cadence == "monthly" and not account.financial_period:
            account.financial_period = account.window_start[:7]
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
        ledger=ledger,
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
    ledger: SignalLedger | None = None,
) -> Analysis:
    scan_id = scan_id or new_scan_id()
    # Governança: candidatos a Producer calculados quando não fornecidos.
    if not account.producer_candidates:
        account.producer_candidates = compute_candidates(account)
    # O rateio da cobrança é derivação pura do que já está no inventário, e
    # rodava só na coleta ao vivo. No caminho do dataset a cobrança chegava e
    # ninguém a distribuía: o achado saía `modeled` com a fatura ali do lado.
    _allocate_billing(account, config)
    account.process_costs = build_process_costs(account, config, today=today)
    graph = build_process_graph(account)
    opportunities = run_all(account, config, scan_id)
    signals = collect_signals(account, config)
    opportunities, signals = _drop_non_recurrent(account, config, opportunities, signals)
    if code_artifacts:
        code_opportunities, code_signals = glue_code.detect(
            account, code_artifacts, config, scan_id
        )
        code_small_file_assets = {
            item.asset_name
            for item in code_opportunities
            if item.rule_id == "GLUE-CODE-SMALL-FILES"
        }
        opportunities = [
            item
            for item in opportunities
            if not (
                item.rule_id == "GLUE-SMALL-FILES-OUTPUT"
                and item.asset_name in code_small_file_assets
            )
        ]
        opportunities += code_opportunities
        signals += code_signals
    protected_signal_keys = {
        (signal.asset_type, signal.asset_name, signal.rule_id) for signal in signals
    }
    investigations: list[Investigation] = []
    # O que a análise contextual já julgou passa para uma fila de investigação
    # separada. Confirmação contextual não cria dinheiro nem backlog.
    if ledger is not None:
        investigations = _build_investigations(
            ledger, signals, account, config
        )
        signals = ledger.suppress(signals, account.account_id).open
    # Processo da plataforma sai daqui, e não da coleta: ele continua no
    # inventário porque o rateio da fatura Glue é proporcional à DPU-hora de
    # cada job — tirá-lo do inventário não tira o consumo dele da fatura, só
    # espalha a parte dele sobre os outros e infla o custo de quem sobrou.
    #
    # O filtro alcança o que **deriva** desses processos, não só o nome deles: o
    # prefixo S3 do event log, a tabela que escrevem, o schedule que os dispara.
    # Casando só o nome, a aplicação da plataforma saía do ranking e voltava
    # como recomendação sobre um artefato dela.
    gerenciados = managed_asset_names(account)
    opportunities = [
        item
        for item in opportunities
        if not _is_managed_finding(item.asset_name, gerenciados)
        and not is_managed(item.source_process or "")
    ]
    signals = [
        item
        for item in signals
        if not _is_managed_finding(item.asset_name, gerenciados)
    ]
    enrich_opportunities(account, graph, opportunities)
    # Consolida achados do mesmo ativo numa ação principal (causa raiz).
    opportunities = group_by_asset(opportunities)
    _link_athena_opportunities(account, opportunities)
    # Aplica realização e caps depois do agrupamento para não reservar custo
    # para achados relacionados que não permanecem no portfólio final.
    apply_conservative_caps(account, opportunities, config, today=today)
    if history is not None:
        apply_calibrations(opportunities, history, config)
        signals.extend(
            Signal(
                kind="trend",
                rule_id="EFFICIENCY-REGRESSION",
                asset_type=f"{item['service']}_process",
                asset_name=item["process"],
                observation=(
                    f"{item['metric']} subiu {item['change']:.1%}: "
                    f"{item['previous']} → {item['current']} {item['unit']}."
                ),
                question="O volume ou resultado útil caiu enquanto o custo unitário subiu?",
                missing_evidence=["volume útil comparável e mudança de demanda"],
            )
            for item in history.efficiency_regressions(account)
        )
    # Ordena por prioridade de execução, com desempate determinístico.
    opportunities.sort(key=ranking_key, reverse=True)

    previous = history.latest_snapshots(account.account_id) if history is not None else []
    reconciliation = Reconciliation()
    # Persistência: preserva status, reabre por evidência e detecta desaparecidas.
    if store is not None:
        from julius.knowledge.rules import disabled_rule_ids

        reconciliation = store.reconcile(
            opportunities,
            scan_id,
            today,
            account_id=account.account_id,
            ignored_rule_ids=disabled_rule_ids(account),
            protected_signal_keys=frozenset(
                protected_signal_keys
                | {
                    (signal.asset_type, signal.asset_name, signal.rule_id)
                    for signal in signals
                }
            ),
        )

    events = compare(previous, opportunities, reconciliation)
    suppressed = set(reconciliation.suppressed)
    opportunities = [
        opportunity
        for opportunity in opportunities
        if opportunity.fingerprint() not in suppressed
    ]
    opportunities.sort(key=ranking_key, reverse=True)

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
    manifest = build_manifest(
        account,
        config,
        scan_id,
        source=source,
        # Eles ficaram fora das recomendações mas continuam no inventário; o
        # manifesto é onde isso é declarado em vez de virar sumiço silencioso.
        # Declara os ativos derivados junto: quem lê o manifesto precisa poder
        # explicar por que um prefixo S3 específico não aparece no relatório.
        managed_processes=sorted(gerenciados),
        opportunity_count=len(opportunities),
    )
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
    vm.ai_investigations = [
        {
            "fingerprint": item.fingerprint,
            "rule_id": item.rule_id,
            "asset_type": item.asset_type,
            "asset_name": item.asset_name,
            "status": item.status,
            "rationale": item.rationale,
            "recommendation": (
                asdict(item.recommendation) if item.recommendation else None
            ),
            "estimation_proposal": (
                asdict(item.proposal) if item.proposal else None
            ),
            "contextual_estimate": (
                asdict(item.estimate) if item.estimate else None
            ),
            "include_in_portfolio": False,
            "classification": "ai_estimate",
        }
        for item in investigations
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
        signals=signals,
        investigations=investigations,
    )


def _allocate_billing(account: Account, config: Config) -> None:
    """Distribui a cobrança coletada entre os ativos, se ainda não foi.

    Idempotente por construção: só roda quando existe cobrança classificada e
    nenhum ativo recebeu rateio ainda. É o que faz o caminho do dataset chegar
    à mesma conclusão do caminho ao vivo — sem isso, ancorar a economia na
    fatura dependia de quem exportou o dataset ter rodado o rateio antes.
    """
    coverage = account.glue_cost_coverage
    if coverage and coverage.buckets and not coverage.allocated_buckets:
        glue_cost.allocate_costs(
            account,
            coverage,
            config,
            allocatable_buckets=config.glue_cost.allocatable_buckets,
            jobs_collection_complete=bool(account.glue_jobs),
        )

    redshift_coverage = getattr(account, "redshift_cost_coverage", None)
    clusters = getattr(account, "redshift_clusters", None) or []
    if (
        redshift_coverage
        and redshift_coverage.buckets
        and not any(c.allocated_compute_cost for c in clusters)
    ):
        redshift_cost.allocate_costs(
            account, redshift_coverage, config.redshift_cost.compute_buckets
        )

    sagemaker_coverage = getattr(account, "sagemaker_cost_coverage", None)
    if sagemaker_coverage and sagemaker_coverage.buckets:
        assets = [
            *account.sagemaker_apps,
            *account.sagemaker_endpoints,
            *account.sagemaker_notebooks,
            *account.sagemaker_jobs,
            *account.sagemaker_feature_groups,
        ]
        if not any(getattr(item, "allocated_cost", None) for item in assets):
            sagemaker_cost.allocate_costs(
                account,
                sagemaker_coverage,
                config.sagemaker_cost.allocatable_buckets,
            )


def _drop_non_recurrent(
    account: Account,
    config: Config,
    opportunities: list[Opportunity],
    signals: list[Signal],
) -> tuple[list[Opportunity], list[Signal]]:
    """Achado de ajuste só vale sobre processo que repete.

    Sobre uma execução única não há média nem perfil — há uma amostra. O que
    sai daqui não é ruído: é recomendação que o produto não consegue sustentar.

    O que é caro e não repete não some: vira sinal, que é hipótese sem economia
    atribuída e fora do ranking. É o único jeito de suprimir a análise sem
    suprimir o dinheiro junto.
    """
    minimo = config.thresholds.recurring_runs_min
    processos = [
        (asset_type, process)
        for asset_type, inventory in (
            ("glue_job", account.glue_jobs),
            ("state_machine", account.state_machines),
        )
        for process in inventory
    ]
    esporadicos = {
        (asset_type, process.name): process
        for asset_type, process in processos
        if not is_recurrent(process, minimo)
    }
    if not esporadicos:
        return opportunities, signals

    # A chave carrega o tipo do ativo, então achado sobre o serviço
    # (`glue_service`) ou sobre uma tabela nunca casa aqui — esta regra é sobre
    # ajuste de processo, e só processo entra no dicionário.
    mantidos = [
        item
        for item in opportunities
        if (item.asset_type, item.asset_name) not in esporadicos
    ]
    caros = [
        _non_recurrent_signal(asset_type, process, minimo)
        for (asset_type, _name), process in esporadicos.items()
        if deserves_signal(process)
    ]
    return mantidos, signals + caros


def _is_managed_finding(asset_name: str, gerenciados: frozenset[str]) -> bool:
    """O achado é sobre um processo da plataforma, ou sobre algo dele?

    O casamento por prefixo existe porque um caminho S3 se subdivide: o event log
    do job é `s3://bucket/logs/<job>/`, e o achado costuma apontar para um
    subprefixo dele. Comparar só por igualdade deixaria passar exatamente o caso
    que motivou esta função.
    """
    nome = str(asset_name or "").strip()
    if not nome:
        return False
    if is_managed(nome) or nome in gerenciados:
        return True
    if not nome.startswith("s3://"):
        return False
    return any(
        alvo.startswith("s3://") and nome.startswith(alvo.rstrip("/") + "/")
        for alvo in gerenciados
    )


def _non_recurrent_signal(asset_type: str, process: Any, minimo: int) -> Signal:
    execucoes = runs_per_month(process)
    consumo = consumption_dpu_hours(process)
    return Signal(
        kind="config",
        rule_id="PROCESS-NON-RECURRING-COST",
        asset_type=asset_type,
        asset_name=process.name,
        observation=(
            f"{execucoes:.1f} execuções/mês (recorrente a partir de {minimo}) "
            f"consumindo {consumo:.1f} DPU-hora na janela"
        ),
        question=(
            "O processo deveria rodar com mais frequência e está falhando em "
            "disparar, ou é pontual por natureza? Se for pontual, o consumo "
            "justifica o que ele entrega? Nenhum ajuste de capacidade é "
            "afirmável sobre este histórico."
        ),
        missing_evidence=[
            f"execuções recorrentes na janela (observadas {execucoes:.1f}/mês)"
        ],
    )


def _build_investigations(
    ledger: SignalLedger,
    signals: list[Signal],
    account: Account,
    config: Config,
) -> list[Investigation]:
    by_fingerprint = {
        signal.fingerprint(account.account_id): signal for signal in signals
    }
    out = []
    for decision in ledger.decisions_for(account.account_id):
        signal = by_fingerprint.get(decision.fingerprint)
        if signal is None or decision.verdict == "rejected":
            continue
        estimate = None
        status = decision.status
        if decision.estimation_proposal is not None:
            try:
                estimate = evaluate_proposal(
                    account, signal, decision.estimation_proposal, config
                )
                status = (
                    "candidate"
                    if estimate.status == "estimated"
                    else "needs_evidence"
                )
            except ValueError as exc:
                estimate = ContextualEstimate(
                    method=decision.estimation_proposal.method,
                    status="rejected",
                    missing_evidence=[str(exc)],
                )
                status = "rejected"
        out.append(
            Investigation(
                fingerprint=decision.fingerprint,
                account=decision.account,
                rule_id=decision.rule_id,
                asset_type=decision.asset_type,
                asset_name=decision.asset_name,
                status=status,
                rationale=decision.rationale,
                recommendation=decision.recommendation,
                proposal=decision.estimation_proposal,
                estimate=estimate,
                evidence_hash=decision.evidence_hash,
                scan_id=decision.scan_id,
                prompt_version=decision.prompt_version,
                decided_at=decision.decided_at,
            )
        )
    return out


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
