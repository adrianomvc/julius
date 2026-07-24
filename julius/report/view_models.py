"""ReportViewModel e OpportunityViewModel — rótulos já formatados.

Nenhuma regra de negócio fica no template: tudo é resolvido aqui.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.governance import recommend
from julius.inventory.model import Account, PreviousResult, ProducerCandidate
from julius.opportunities.base import Opportunity
from julius.report import formatters as fmt
from julius.report.pareto import Pareto, compute as compute_pareto

_HIGH_CONF = 0.80


@dataclass
class OpportunityVM:
    id: str
    title: str
    asset: str
    category_label: str
    category_fg: str
    category_bg: str
    monthly_fmt: str
    band_fmt: str
    year_fmt: str
    difficulty: int
    difficulty_label: str
    diff_fg: str
    diff_bg: str
    confidence_label: str
    conf_fg: str
    conf_bg: str
    exec_priority: int
    exec_w: str
    exec_color: str
    strat_priority: int
    strat_w: str
    strat_color: str
    bucket: str
    bucket_label: str
    bucket_fg: str
    bucket_bg: str
    action: str
    why: str
    how_to_apply: str
    how_to_validate: str
    evidence: list[str]
    doc_url: str
    owner: str
    owner_source: str
    actor: str | None
    coverage_pct: str
    sources: str
    actionable: bool
    next_action: str | None
    is_strategic: bool
    source_process: str | None
    ai_diagnosis: str = ""
    ai_recommendation: str = ""
    ai_implementation_steps: list[str] = field(default_factory=list)
    ai_validation_steps: list[str] = field(default_factory=list)
    ai_dependencies: list[str] = field(default_factory=list)
    ai_conflicts: list[str] = field(default_factory=list)
    ai_risks: list[str] = field(default_factory=list)
    ai_documentation: list[dict] = field(default_factory=list)
    ai_assumptions: list[str] = field(default_factory=list)
    ai_missing_evidence: list[str] = field(default_factory=list)


@dataclass
class ProducerVM:
    name: str
    cand: int
    ready: int
    cand_w: str
    ready_w: str
    x: str  # posição no eixo prontidão (horizontal)
    y: str  # posição no eixo candidatura (vertical)
    color: str
    rec_label: str
    note: str


@dataclass
class PreviousResultVM:
    title: str
    asset: str
    date: str
    predicted_fmt: str
    realized_fmt: str
    precision: int
    prec_fg: str
    prec_bg: str
    unit: str


def _producer_vm(p: ProducerCandidate) -> ProducerVM:
    rec = recommend(p.candidate_score, p.readiness_score)
    return ProducerVM(
        name=p.name,
        cand=p.candidate_score,
        ready=p.readiness_score,
        cand_w=f"{p.candidate_score}%",
        ready_w=f"{p.readiness_score}%",
        x=f"{p.readiness_score}%",
        y=f"{p.candidate_score}%",
        color=rec.color,
        rec_label=rec.label,
        note=rec.note,
    )


def _prev_vm(r: PreviousResult, currency: str) -> PreviousResultVM:
    p = r.precision
    if p >= 85:
        fg, bg = "#e8730c", "#fdefdd"
    elif p >= 70:
        fg, bg = "#b26a12", "#faf0dd"
    else:
        fg, bg = "#b23b3b", "#f7e4e4"
    return PreviousResultVM(
        title=r.title,
        asset=r.asset,
        date=r.date,
        predicted_fmt=fmt.money(r.predicted_monthly, currency) + "/mês",
        realized_fmt=fmt.money(r.realized_monthly, currency) + "/mês",
        precision=p,
        prec_fg=fg,
        prec_bg=bg,
        unit=r.unit,
    )


def _mask(account_id: str) -> str:
    # Mascara apenas IDs numéricos de conta AWS (12 dígitos); nomes amigáveis
    # (ex.: "consumer-avi") passam intactos.
    if account_id.isdigit() and len(account_id) == 12:
        return account_id[:4] + "…" + account_id[-3:]
    return account_id


def _opp_vm(o: Opportunity, currency: str) -> OpportunityVM:
    cat_label, cat_fg, cat_bg = fmt.CATEGORY_LABELS.get(
        o.asset_type, (o.asset_type, "#5b6169", "#eef0ea")
    )
    diff_fg, diff_bg = fmt.difficulty_color(o.difficulty_score)
    conf_fg, conf_bg = fmt.confidence_color(o.confidence_label)
    bucket_fg, bucket_bg = fmt.BUCKET_COLORS.get(o.bucket, ("#5b6169", "#eef0ea"))
    g = o.estimated_gain
    return OpportunityVM(
        id=o.opportunity_id,
        title=o.finding,
        asset=f"{o.asset_type}: {o.asset_name}",
        category_label=cat_label,
        category_fg=cat_fg,
        category_bg=cat_bg,
        monthly_fmt=fmt.money(g.monthly_expected, currency) if not g.is_strategic else "Estratégico",
        band_fmt=(
            f"{fmt.money(g.monthly_low, currency)}–{fmt.money(g.monthly_high, currency)}" if not g.is_strategic else "—"
        ),
        year_fmt=fmt.money(g.realizable_year, currency) if not g.is_strategic else "Estratégico",
        difficulty=o.difficulty_score,
        difficulty_label=_diff_label(o.difficulty_score),
        diff_fg=diff_fg,
        diff_bg=diff_bg,
        confidence_label=o.confidence_label,
        conf_fg=conf_fg,
        conf_bg=conf_bg,
        exec_priority=o.execution_priority,
        exec_w=f"{o.execution_priority}%",
        exec_color=fmt.exec_color(o.execution_priority),
        strat_priority=o.strategic_priority,
        strat_w=f"{o.strategic_priority}%",
        strat_color=fmt.strat_color(o.strategic_priority),
        bucket=o.bucket,
        bucket_label=fmt.BUCKET_LABELS.get(o.bucket, o.bucket),
        bucket_fg=bucket_fg,
        bucket_bg=bucket_bg,
        action=o.recommended_action,
        why=o.why,
        how_to_apply=o.how_to_apply,
        how_to_validate=o.how_to_validate,
        evidence=o.evidence,
        doc_url=o.doc_links[0] if o.doc_links else "",
        owner=o.owner or "não identificado",
        owner_source=o.owner_source,
        actor=o.actor,
        coverage_pct=f"{round(o.evidence_coverage * 100)}%",
        sources=", ".join(o.data_sources),
        actionable=o.actionable,
        next_action=o.next_action,
        is_strategic=g.is_strategic,
        source_process=o.source_process,
    )


def _diff_label(difficulty: int) -> str:
    from julius.opportunities.effort import label

    return label(difficulty)


@dataclass
class ReportViewModel:
    account_id: str
    account_masked: str
    region: str
    period: str
    lookback: str
    scan_id: str
    generated_at: str
    currency: str

    total_cost_fmt: str
    identified_fmt: str
    high_conf_fmt: str
    realizable_year_fmt: str
    recommendation: str

    kpi_total: int
    kpi_actionable: int
    kpi_strategic: int
    pareto_pct: int
    pareto_count: int
    pareto_sentence: str
    pareto_sum_fmt: str
    monthly_total_fmt: str
    pareto_bar: list[dict]
    executable_pct: int
    executable_count: int
    months_remaining: int

    services: list[dict]
    account_total_fmt: str
    account_saving_fmt: str
    account_saving_pct: str
    account_bar_w: str

    focus: list[OpportunityVM]
    table: list[OpportunityVM]
    do_now: list[OpportunityVM]
    plan: list[OpportunityVM]
    monitor: list[OpportunityVM]
    investigate: list[OpportunityVM]
    producers: list[ProducerVM] = field(default_factory=list)
    previous_results: list[PreviousResultVM] = field(default_factory=list)
    manifest: list[dict] = field(default_factory=list)
    diff_events: list[dict] = field(default_factory=list)
    committed_fmt: str = "R$ 0"
    realized_fmt: str = "R$ 0"
    realization_rate_pct: str = "—"
    lifecycle_lead_times: list[dict] = field(default_factory=list)
    ai_summary: str = ""
    ai_implementation_order: list[dict] = field(default_factory=list)
    ai_recommendations: list[dict] = field(default_factory=list)
    athena_coverage: dict = field(default_factory=dict)
    athena_queries: list[dict] = field(default_factory=list)
    athena_actors: list[dict] = field(default_factory=list)
    athena_gaps: list[str] = field(default_factory=list)


def _recommendation(do_now: list[Opportunity], currency: str) -> str:
    picks = sorted(
        [o for o in do_now if o.actionable and not o.estimated_gain.is_strategic],
        key=lambda o: o.execution_priority,
        reverse=True,
    )[:2]
    if not picks:
        return (
            "Ainda não há ação de baixo risco pronta — o próximo passo é coletar evidência "
            "nas oportunidades de maior potencial."
        )
    names = " e ".join(o.asset_name for o in picks)
    combined = sum(o.estimated_gain.monthly_expected for o in picks)
    return (
        f"Começar por {names} — mudanças isoladas, baixa dificuldade, economia combinada estimada de "
        f"{fmt.money(combined, currency)}/mês. Estimativa a validar após a mudança."
    )


def _services(account: Account, opportunities: list[Opportunity]) -> tuple[list[dict], float]:
    """Reconciliação: custo por serviço + economia identificada mapeada a cada serviço."""
    service_of = {
        "glue_job": "AWS Glue",
        "glue_session": "AWS Glue",
        "athena_query": "Amazon Athena",
        "s3": "Amazon S3",
    }
    saving_by_service: dict[str, float] = {}
    for o in opportunities:
        if o.estimated_gain.is_strategic:
            continue
        svc = service_of.get(o.asset_type)
        if svc:
            saving_by_service[svc] = saving_by_service.get(svc, 0.0) + o.estimated_gain.monthly_expected

    total = account.total_monthly_cost or 1.0
    rows = []
    for s in account.services:
        saving = saving_by_service.get(s.name, 0.0)
        rows.append(
            {
                "name": s.name,
                "sub": s.subtitle,
                "cost_fmt": fmt.money(s.monthly_cost, account.currency),
                "saving_fmt": fmt.money(saving, account.currency) if saving > 0 else "—",
                "saving_color": "#e8730c" if saving > 0 else "#b6bcae",
                "pct_opt": f"{round(saving / s.monthly_cost * 100)}%" if saving > 0 and s.monthly_cost else "—",
                "bar_base_w": f"{s.monthly_cost / total * 100:.1f}%",
                "bar_save_w": f"{saving / total * 100:.1f}%",
            }
        )
    return rows, sum(saving_by_service.values())


def _pareto_bar(pareto: Pareto, currency: str) -> list[dict]:
    total = pareto.monthly_total or 1.0
    segments: list[dict] = []
    for i, o in enumerate(pareto.financial_focus):
        g = o.estimated_gain.monthly_expected
        segments.append(
            {
                "w": f"{g / total * 100:.1f}%",
                "color": fmt.PARETO_SEGMENT_COLORS[i % len(fmt.PARETO_SEGMENT_COLORS)],
                "title": f"{o.finding} · {fmt.money(g, currency)}",
            }
        )
    remainder = pareto.monthly_total - pareto.financial_sum
    if remainder > 0:
        segments.append(
            {
                "w": f"{remainder / total * 100:.1f}%",
                "color": fmt.PARETO_REMAINDER_COLOR,
                "title": f"Demais oportunidades · {fmt.money(remainder, currency)}",
            }
        )
    return segments


def _athena_views(account: Account) -> tuple[dict, list[dict], list[dict], list[str]]:
    coverage = account.athena_coverage
    if coverage is None:
        return {}, [], [], []
    coverage_vm = {
        "window": f"{coverage.window_start[:10]} → {coverage.window_end[:10]} UTC",
        "workgroups": f"{coverage.workgroups_covered}/{coverage.workgroups_total}",
        "truncated": coverage.truncated,
        "cost_quality": coverage.cost_quality,
        "cost_fmt": fmt.money(coverage.net_cost, coverage.currency),
        "ratio": f"{coverage.reconciliation_ratio * 100:.1f}%"
        if coverage.reconciliation_ratio is not None else "—",
    }
    query_rows = [
        {
            "fingerprint": q.structural_fingerprint or q.query_id,
            "workgroup": q.workgroup,
            "executions": q.observed_runs,
            "active_days": q.active_days,
            "cost_fmt": fmt.money(q.allocated_cost, q.currency),
            "billed_gb": f"{q.billed_bytes / 1024**3:.2f}",
            "recurrence": ", ".join(
                label for flag, label in (
                    (q.recurring, "recorrente"), (q.burst, "burst"), (q.regular, "regular")
                ) if flag
            ) or "ocasional",
            "failures": q.failed_runs + q.cancelled_runs,
            "reuse": q.reused_runs,
            "reuse_eligible": q.reuse_eligible_runs,
            "reuse_avoidable_cost_fmt": fmt.money(
                q.reuse_avoidable_cost, q.currency
            ),
            "sql": q.statement,
            "wide_tables": list(q.wide_tables),
            "full_scan": q.full_scan_confirmed,
            "unpartitioned": list(q.unpartitioned_tables),
            "uncompressed": list(
                q.row_format_uncompressed + q.columnar_uncompressed
            ),
            "codecs": list(q.compression_codecs),
            "projection_candidates": list(q.partition_projection_candidates),
        }
        for q in account.athena_queries[:20]
    ]
    actor_rows = [
        {
            "actor": actor.actor,
            "type": "automação" if actor.automated else actor.actor_type,
            "queries": actor.query_count,
            "active_days": actor.active_days,
            "cost_fmt": fmt.money(actor.allocated_cost, actor.currency),
            "recurring": actor.recurring_patterns,
            "bursts": actor.bursts,
            "selects_star": actor.selects_star,
            "missing_partition": actor.missing_partition_filters,
            "failures": actor.failures,
            "full_scans": actor.full_scans,
            "unpartitioned": actor.unpartitioned_tables,
            "compression": actor.compression_findings,
            "projection": actor.partition_projection_candidates,
            "opportunity_refs": list(actor.opportunity_refs),
            "guidance": actor.query_count >= 3
            and (
                actor.selects_star
                + actor.missing_partition_filters
                + actor.full_scans
                + actor.unpartitioned_tables
                + actor.compression_findings
                + actor.partition_projection_candidates
                + actor.failures
            ) >= 3,
        }
        for actor in account.athena_actor_usage
    ]
    return coverage_vm, query_rows, actor_rows, list(coverage.gaps)


def build(
    account: Account,
    opportunities: list[Opportunity],
    manifest: list[dict],
) -> ReportViewModel:
    from julius.estimation import months_remaining_in_year

    pareto: Pareto = compute_pareto(opportunities)

    identified = sum(
        o.estimated_gain.monthly_expected
        for o in opportunities
        if not o.estimated_gain.is_strategic
    )
    high_conf = sum(
        o.estimated_gain.monthly_expected
        for o in opportunities
        if not o.estimated_gain.is_strategic and o.confidence >= _HIGH_CONF
    )
    realizable_year = sum(o.estimated_gain.realizable_year for o in opportunities)

    do_now = [o for o in opportunities if o.bucket == "fazer_agora"]
    plan = [o for o in opportunities if o.bucket == "planejar"]
    monitor = [o for o in opportunities if o.bucket == "monitorar"]
    investigate = [o for o in opportunities if o.bucket == "investigar_primeiro"]

    services, _ = _services(account, opportunities)
    athena_coverage, athena_queries, athena_actors, athena_gaps = _athena_views(account)

    table_sorted = sorted(opportunities, key=lambda o: o.execution_priority, reverse=True)

    return ReportViewModel(
        account_id=account.account_id,
        account_masked=_mask(account.account_id),
        region=account.region,
        period=account.period,
        lookback=f"{account.lookback_days} dias",
        scan_id=manifest_val(manifest, "scan_id"),
        generated_at=account.generated_at,
        currency=account.currency,
        total_cost_fmt=fmt.money(account.total_monthly_cost, account.currency),
        identified_fmt=fmt.money(identified, account.currency),
        high_conf_fmt=fmt.money(high_conf, account.currency),
        realizable_year_fmt=fmt.money(realizable_year, account.currency),
        recommendation=_recommendation(do_now, account.currency),
        kpi_total=len(opportunities),
        kpi_actionable=len([o for o in opportunities if o.actionable]),
        kpi_strategic=len([o for o in opportunities if o.estimated_gain.is_strategic]),
        pareto_pct=pareto.financial_pct,
        pareto_count=len(pareto.financial_focus),
        pareto_sentence=(
            f"{pareto.financial_pct}% da economia está em {len(pareto.financial_focus)} "
            f"oportunidades ({fmt.money(pareto.financial_sum, account.currency)} de "
            f"{fmt.money(pareto.monthly_total, account.currency)})."
        ),
        pareto_sum_fmt=fmt.money(pareto.financial_sum, account.currency),
        monthly_total_fmt=fmt.money(pareto.monthly_total, account.currency),
        pareto_bar=_pareto_bar(pareto, account.currency),
        executable_pct=pareto.executable_pct,
        executable_count=len(pareto.executable_focus),
        months_remaining=months_remaining_in_year(),
        services=services,
        account_total_fmt=fmt.money(account.total_monthly_cost, account.currency),
        account_saving_fmt=fmt.money(identified, account.currency),
        account_saving_pct=(
            f"{round(identified / account.total_monthly_cost * 100)}%"
            if account.total_monthly_cost
            else "—"
        ),
        account_bar_w=(
            f"{identified / account.total_monthly_cost * 100:.1f}%"
            if account.total_monthly_cost
            else "0%"
        ),
        focus=[_opp_vm(o, account.currency) for o in pareto.financial_focus],
        table=[_opp_vm(o, account.currency) for o in table_sorted],
        do_now=[_opp_vm(o, account.currency) for o in do_now],
        plan=[_opp_vm(o, account.currency) for o in plan],
        monitor=[_opp_vm(o, account.currency) for o in monitor],
        investigate=[_opp_vm(o, account.currency) for o in investigate],
        producers=[_producer_vm(p) for p in account.producer_candidates],
        previous_results=[_prev_vm(r, account.currency) for r in account.previous_results],
        manifest=manifest,
        athena_coverage=athena_coverage,
        athena_queries=athena_queries,
        athena_actors=athena_actors,
        athena_gaps=athena_gaps,
    )


def manifest_val(manifest: list[dict], key: str) -> str:
    for item in manifest:
        if item.get("k") == key:
            return str(item.get("v", ""))
    return ""
