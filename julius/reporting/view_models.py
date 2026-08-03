"""ReportViewModel e OpportunityViewModel — rótulos já formatados.

Nenhuma regra de negócio fica no template: tudo é resolvido aqui.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models import Account, PreviousResult, ProducerCandidate
from julius.findings.opportunity import Opportunity
from julius.governance import recommend
from julius.knowledge.rules import families_without_evidence, missing_evidence
from julius.reporting import formatters as fmt
from julius.reporting.pareto import Pareto
from julius.reporting.pareto import compute as compute_pareto
from julius.scoring import evidence_quality
from julius.scoring.priority import ranking_key

_HIGH_CONF = 0.80


@dataclass
class OpportunityVM:
    id: str
    title: str
    asset: str
    # O tipo é vocabulário interno e não se mostra ao analista, mas monta o ARN
    # e atribui a economia ao serviço — os dois lugares onde o nome solto do
    # ativo não basta.
    asset_type: str
    # O mesmo valor de `monthly_fmt`, sem formatar. Somar texto formatado é
    # frágil, e o relatório precisa somar subconjuntos da lista.
    monthly: float
    # `rule` ou `ai_confirmed`: de qual camada o achado veio.
    origin: str
    category_label: str
    category_fg: str
    category_bg: str
    monthly_fmt: str
    technical_fmt: str
    calibrated_fmt: str
    calibration_note: str
    potential_fmt: str
    gain_class: str
    baseline_fmt: str
    baseline_quality_label: str
    saving_quality: str
    saving_quality_label: str
    # A escala comparável, derivada do elo mais fraco entre baseline e economia.
    evidence_quality: str
    evidence_quality_label: str
    anchored_in_billing: bool
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
    # As premissas do cálculo. O HTML não as mostra por linha, mas a planilha
    # sim: "de onde veio esse número" é onde a conversa costuma terminar.
    assumptions: list[str]
    doc_url: str
    owner: str
    owner_source: str
    owner_event_time: str
    actor: str | None
    coverage_pct: str
    sources: str
    actionable: bool
    next_action: str | None
    is_strategic: bool
    source_process: str | None
    process_cost_fmt: str
    process_monthly_fmt: str
    window_end: str
    traceability: str
    include_in_portfolio: bool
    portfolio_exclusion_reasons: list[str]
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


def _prev_vm(r: PreviousResult) -> PreviousResultVM:
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
        predicted_fmt=fmt.money(r.predicted_monthly, r.currency) + "/mês",
        realized_fmt=fmt.money(r.realized_monthly, r.currency) + "/mês",
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
    technical = o.estimated_gain
    g = o.portfolio_gain
    estimation = o.estimation
    saving_quality = estimation.saving_quality if estimation else "unavailable"
    baseline_quality = estimation.baseline_quality if estimation else "unavailable"
    # A escala única resolve o que os três vocabulários paralelos não
    # respondiam: entre dois achados, qual é o mais confiável.
    overall = evidence_quality.EvidenceQuality[o.evidence_quality.upper()]
    saving_unavailable = saving_quality == "unavailable"
    trace_parts = []
    for ref in o.evidence_refs:
        ids = ref.get("run_ids") or ref.get("execution_ids") or []
        suffix = f" · {len(ids)} execução(ões)" if ids else ""
        if ids:
            suffix += " · " + ", ".join(str(item) for item in ids[:3])
        trace_parts.append(f"{ref.get('source', 'evidência')}{suffix}")
    return OpportunityVM(
        id=o.opportunity_id,
        title=o.finding,
        # O chip de categoria já diz o serviço; repetir o tipo aqui só gasta
        # a linha com vocabulário interno.
        asset=o.asset_name,
        asset_type=o.asset_type,
        monthly=g.monthly_expected if o.include_in_portfolio else 0.0,
        origin=o.origin,
        category_label=cat_label,
        category_fg=cat_fg,
        category_bg=cat_bg,
        monthly_fmt=(
            "Indisponível"
            if saving_unavailable
            else fmt.money(g.monthly_expected, currency)
            if o.calibrated_gain is not None
            else fmt.money(
                estimation.estimated_saving if estimation else 0, currency
            )
            if estimation and estimation.saving_quality == "modeled_rule"
            else fmt.money(g.monthly_expected, currency)
            if not g.is_strategic
            else "Estratégico"
        ),
        technical_fmt=(
            "—"
            if technical.is_strategic
            else fmt.money(technical.monthly_expected, currency)
        ),
        calibrated_fmt=(
            fmt.money(o.calibrated_gain.monthly_expected, currency)
            if o.calibrated_gain is not None
            else "Sem amostra suficiente"
        ),
        calibration_note=(
            (
                f"{o.calibration.sample_count} casos · fator "
                f"{o.calibration.factor_expected:.3f} · "
                f"{o.calibration.confidence} · {o.calibration.fallback_level}"
            )
            if o.calibration is not None
            else ""
        ),
        potential_fmt=(
            fmt.money(estimation.estimated_saving, currency)
            if estimation
            and estimation.estimated_saving > 0
            and not saving_unavailable
            else "—"
        ),
        gain_class=(
            "indisponível"
            if saving_unavailable
            else "potencial bloqueado"
            if g.is_strategic
            else "ganho comprovado/modelado"
        ),
        baseline_fmt=(
            fmt.money(estimation.baseline_cost, currency)
            if estimation and estimation.baseline_cost > 0
            else "—"
        ),
        baseline_quality_label=evidence_quality.from_baseline(baseline_quality).label,
        saving_quality=saving_quality,
        saving_quality_label=evidence_quality.from_saving(saving_quality).label,
        evidence_quality=overall.name.lower(),
        evidence_quality_label=overall.label,
        anchored_in_billing=overall.is_anchored_in_billing,
        band_fmt=(
            "—"
            if saving_unavailable
            else f"{fmt.money(g.monthly_low, currency)}–{fmt.money(g.monthly_high, currency)}"
            if not g.is_strategic
            else "—"
        ),
        year_fmt=(
            "—"
            if saving_unavailable
            else fmt.money(g.realizable_year, currency)
            if not g.is_strategic
            else "Estratégico"
        ),
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
        assumptions=list(estimation.assumptions) if estimation else [],
        doc_url=o.doc_links[0] if o.doc_links else "",
        owner=o.owner or "não identificado",
        owner_source=o.owner_source,
        owner_event_time=o.owner_event_time or "—",
        actor=o.actor,
        coverage_pct=f"{round(o.evidence_coverage * 100)}%",
        sources=", ".join(o.data_sources),
        actionable=o.actionable,
        next_action=o.next_action,
        is_strategic=g.is_strategic,
        source_process=o.source_process,
        process_cost_fmt=(
            fmt.money(o.process_cost_window, currency)
            if o.process_cost_window is not None
            else "—"
        ),
        process_monthly_fmt=(
            fmt.money(o.process_cost_monthly, currency)
            if o.process_cost_monthly is not None
            else "—"
        ),
        window_end=o.window_end or "—",
        traceability="; ".join(trace_parts) or "sem IDs de execução disponíveis",
        include_in_portfolio=o.include_in_portfolio,
        portfolio_exclusion_reasons=list(o.portfolio_exclusion_reasons),
    )


def _diff_label(difficulty: int) -> str:
    from julius.scoring.difficulty import label

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
    technical_identified_fmt: str
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
    process_costs: list[dict]
    process_cost_attributed_fmt: str
    glue_cost_explorer_fmt: str
    process_cost_delta_fmt: str
    process_reconciliation_note: str
    collection_status: str
    collection_status_label: str
    collection_status_fg: str
    collection_status_bg: str
    collection_health_summary: str
    collection_health: list[dict]
    # Famílias de regra cujo inventário chegou vazio: o relatório precisa dizer
    # que ali não houve o que analisar, senão o silêncio se lê como "tudo ok".
    rule_families_without_evidence: list[dict]
    inventory_integrity: list[dict]

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
    committed_fmt: str = "US$ 0.00"
    realized_fmt: str = "US$ 0.00"
    realization_rate_pct: str = "—"
    lifecycle_lead_times: list[dict] = field(default_factory=list)
    ai_summary: str = ""
    ai_implementation_order: list[dict] = field(default_factory=list)
    ai_recommendations: list[dict] = field(default_factory=list)
    ai_coverage: dict = field(default_factory=dict)
    # IDs do foco 80/20. A lista de recomendações é ordenada por
    # prioridade, então o Pareto não pode ser o prefixo dela — ele vira
    # marcador em cada item, e o leitor continua vendo onde está a
    # concentração sem uma segunda lista repetindo a primeira.
    focus_ids: list[str] = field(default_factory=list)
    #: Hipóteses ainda em aberto, com ordem de grandeza e sem cifra. Ficam fora
    #: de `table` e dos totais de propósito: o que decide prioridade entre elas
    #: é a faixa, e o que decide o portfólio é a economia — não são a mesma
    #: coisa e não podem somar na mesma linha.
    signals: list[dict] = field(default_factory=list)
    ai_signal_verdicts: list[dict] = field(default_factory=list)
    ai_uncovered_findings: list[dict] = field(default_factory=list)
    #: Instrução dirigida ao agente encontrada dentro de um artefato analisado.
    #: Não é achado de custo: é registro de que alguém escreveu aquilo ali. O
    #: texto é dado hostil por definição e o template o escapa como qualquer
    #: outro conteúdo — ele nunca é reinterpretado como instrução.
    ai_suspected_injections: list[dict] = field(default_factory=list)
    ai_investigations: list[dict] = field(default_factory=list)
    athena_coverage: dict = field(default_factory=dict)
    athena_queries: list[dict] = field(default_factory=list)
    athena_actors: list[dict] = field(default_factory=list)
    athena_gaps: list[str] = field(default_factory=list)
    glue_cost: dict = field(default_factory=dict)
    s3_cost: dict = field(default_factory=dict)


def _recommendation(do_now: list[Opportunity], currency: str) -> str:
    picks = sorted(
        [o for o in do_now if o.actionable and o.include_in_portfolio],
        key=ranking_key,
        reverse=True,
    )[:2]
    if not picks:
        return (
            "Ainda não há ação de baixo risco pronta — o próximo passo é coletar evidência "
            "nas oportunidades de maior potencial."
        )
    names = " e ".join(o.asset_name for o in picks)
    combined = sum(o.portfolio_gain.monthly_expected for o in picks)
    return (
        f"Começar por {names} — mudanças isoladas, baixa dificuldade, economia combinada estimada de "
        f"{fmt.money(combined, currency)}/mês. Estimativa a validar após a mudança."
    )


def _services(account: Account, opportunities: list[Opportunity]) -> tuple[list[dict], float]:
    """Reconciliação: custo por serviço + economia identificada mapeada a cada serviço."""
    # Todo tipo de ativo que o catálogo produz precisa cair num serviço. Este
    # mapa cobria quatro de doze, e o efeito era grave: a economia de Redshift,
    # SageMaker, S3 e Step Functions — US$ 1.253,62 no dataset de exemplo —
    # aparecia como "—" na tabela, ao lado de recomendações que a reivindicavam
    # na mesma página.
    service_of = {
        "glue_job": "AWS Glue",
        "glue_session": "AWS Glue",
        "glue_crawler": "AWS Glue",
        "glue_service": "AWS Glue",
        "databrew_job": "AWS Glue",
        "athena_query": "Amazon Athena",
        "athena_workgroup": "Amazon Athena",
        "state_machine": "Step Functions",
        "sagemaker_app": "Amazon SageMaker",
        "sagemaker_endpoint": "Amazon SageMaker",
        "redshift_cluster": "Amazon Redshift",
        "s3": "Amazon S3",
        "s3_bucket": "Amazon S3",
        "s3_prefix": "Amazon S3",
        # A economia de uma tabela sem uso é o compute do job que a escreve.
        "table": "AWS Glue",
    }
    saving_by_service: dict[str, float] = {}
    saving_currency_by_service: dict[str, str] = {}
    for o in opportunities:
        if not o.include_in_portfolio:
            continue
        svc = service_of.get(o.asset_type)
        if svc:
            saving_by_service[svc] = saving_by_service.get(svc, 0.0) + o.portfolio_gain.monthly_expected
            if o.estimation is not None:
                saving_currency_by_service[svc] = o.estimation.currency

    total = account.billing_cost_mtd or 1.0
    rows = []
    for s in account.services:
        saving = saving_by_service.get(s.name, 0.0)
        comparable = saving_currency_by_service.get(s.name, s.currency) == s.currency
        if not comparable:
            saving = 0.0
        rows.append(
            {
                "name": s.name,
                "sub": (
                    f"{s.subtitle} · MTD até {s.data_through or '—'}"
                    f"{' · estimado pela AWS' if s.estimated else ''}"
                    + (
                        f" · forecast AWS {fmt.money(s.forecast_cost_eom, s.currency)}"
                        if s.forecast_cost_eom is not None
                        else ""
                    )
                ),
                "cost_fmt": fmt.money(s.monthly_cost, s.currency),
                "saving_fmt": fmt.money(saving, s.currency) if saving > 0 else "—",
                "saving_color": "#e8730c" if saving > 0 else "#b6bcae",
                "pct_opt": f"{round(saving / s.monthly_cost * 100)}%" if saving > 0 and s.monthly_cost else "—",
                "bar_base_w": f"{s.monthly_cost / total * 100:.1f}%",
                "bar_save_w": f"{saving / total * 100:.1f}%",
            }
        )

    # Um serviço pode ter economia sem ter linha própria no Cost Explorer: no
    # dataset de exemplo o SageMaker está dentro de "Outros". Antes, essa
    # economia sumia da tabela e continuava contada no total — o relatório
    # prometia um valor que a primeira seção não explicava. Agora ela aparece
    # com o custo em branco, que é a informação verdadeira: sabemos o que dá
    # para economizar, não sabemos quanto o serviço custa isolado.
    listados = {s.name for s in account.services}
    for nome, saving in saving_by_service.items():
        if nome in listados or saving <= 0:
            continue
        moeda = saving_currency_by_service.get(nome, account.currency)
        rows.append(
            {
                "name": nome,
                "sub": "custo não separado na fatura coletada",
                "cost_fmt": "—",
                "saving_fmt": fmt.money(saving, moeda),
                "saving_color": "#e8730c",
                "pct_opt": "—",
                "bar_base_w": "0.0%",
                "bar_save_w": f"{saving / total * 100:.1f}%",
            }
        )
    return rows, sum(saving_by_service.values())


def _process_costs(account: Account) -> list[dict]:
    return [
        {
            "name": row.process_name,
            "root_type": row.root_type,
            "owner": row.owner or "não identificado",
            "owner_source": row.owner_source,
            "owner_event_time": row.owner_event_time or "—",
            "owner_event_name": row.owner_event_name or "—",
            "cost_window": fmt.money(row.total_cost_window, row.currency),
            "cost_monthly": fmt.money(row.monthly_cost, row.currency),
            "actual_cost_window_value": row.actual_cost_window,
            "estimated_cost_window_value": row.estimated_cost_window,
            "total_cost_window_value": row.total_cost_window,
            "monthly_cost_value": row.monthly_cost,
            "currency": row.currency,
            "window_start": row.window_start,
            "window_end": row.window_end or "—",
            "window_days": row.window_days,
            "dpu_actual": f"{row.actual_dpu_hours:.2f}",
            "dpu_estimated": f"{row.estimated_dpu_hours:.2f}",
            "allocation": row.allocation_method,
            "components": ", ".join(row.component_names),
        }
        for row in account.process_costs
    ]


def _cost_reconciliation(account: Account) -> tuple[str, str, str, str]:
    """Custo atribuído × cobrança, ambos **na janela de análise**.

    A cobrança comparável é a do Cost Explorer coletada na mesma janela, não a
    do painel de fatura: aquela é mês-calendário parcial e a diferença entre os
    dois períodos apareceria como divergência de atribuição.
    """
    process_total = sum(row.total_cost_window for row in account.process_costs)
    attributed = fmt.usd(process_total)
    coverage = account.glue_cost_coverage
    if coverage is None or coverage.net_cost is None:
        return (
            attributed,
            "—",
            "—",
            "cobrança Glue da janela não coletada; sem base para reconciliar.",
        )
    delta = coverage.net_cost - process_total
    note = (
        f"Cobrança Glue de {coverage.period_start} a {coverage.data_through}, "
        f"qualidade {coverage.cost_quality}; a diferença reúne tarifas, "
        "arredondamento e consumo ainda não atribuído."
    )
    if coverage.unattributed_cost:
        note += f" Buckets sem rateio somam {fmt.usd(coverage.unattributed_cost)}."
    return attributed, fmt.usd(coverage.net_cost), fmt.usd(delta), note


def _glue_cost_view(account: Account) -> dict:
    """Qualidade e composição da cobrança Glue, ao lado do bloco Athena."""
    coverage = account.glue_cost_coverage
    if coverage is None:
        return {}
    return {
        "window": f"{coverage.period_start} → {coverage.data_through}",
        "cost_quality": coverage.cost_quality,
        "cost_metric": coverage.cost_metric or "—",
        "cost_fmt": fmt.usd(coverage.net_cost),
        "unattributed_fmt": fmt.usd(coverage.unattributed_cost),
        "ratio": f"{coverage.modeled_ratio * 100:.1f}%"
        if coverage.modeled_ratio is not None else "—",
        "buckets": [
            {"name": name, "cost_fmt": fmt.usd(value)}
            for name, value in sorted(
                coverage.buckets.items(), key=lambda item: item[1], reverse=True
            )
        ],
        "unknown_usage_types": list(coverage.unknown_usage_types),
        "gaps": list(coverage.gaps),
    }


def _s3_cost_view(account: Account) -> dict:
    coverage = account.s3_cost_coverage
    if coverage is None:
        return {}
    groups = [
        {
            "kind": kind,
            "cost": coverage.cost_for({bucket}),
            "cost_fmt": fmt.usd(coverage.cost_for({bucket})),
            "quantity": coverage.quantity_for({bucket}),
            "quantity_fmt": (
                f"{coverage.quantity_for({bucket}):,.0f} requests"
                if coverage.quantity_for({bucket}) is not None
                else "—"
            ),
            "cost_per_thousand_fmt": (
                fmt.usd((coverage.unit_cost_for({bucket}) or 0.0) * 1000)
                if coverage.unit_cost_for({bucket}) is not None
                else "—"
            ),
        }
        for kind, bucket in (
            ("GET/read", "requests_read"),
            ("PUT/write", "requests_write"),
            ("outras", "requests_other"),
        )
    ]
    return {
        "window": f"{coverage.period_start} → {coverage.data_through}",
        "cost_quality": coverage.cost_quality,
        "cost_metric": coverage.cost_metric or "—",
        "cost_fmt": fmt.usd(coverage.net_cost),
        "request_groups": groups,
        "request_cost_fmt": fmt.usd(
            coverage.cost_for(
                {"requests_read", "requests_write", "requests_other"}
            )
        ),
        "lines": [
            {
                "usage_type": line.usage_type,
                "bucket": line.bucket,
                "cost": line.cost,
                "cost_fmt": fmt.usd(line.cost),
                "usage_quantity": line.usage_quantity,
                "usage_unit": line.usage_unit,
                "usage_fmt": (
                    f"{line.usage_quantity:,.2f} {line.usage_unit}".strip()
                    if line.usage_quantity is not None
                    else "—"
                ),
            }
            for line in coverage.lines
        ],
        "gaps": list(coverage.gaps),
    }


def _pareto_bar(pareto: Pareto, currency: str) -> list[dict]:
    total = pareto.monthly_total or 1.0
    segments: list[dict] = []
    for i, o in enumerate(pareto.financial_focus):
        g = o.portfolio_gain.monthly_expected
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


def _lookback(account: Account) -> str:
    """A janela pedida, dizendo quando ela é de bootstrap.

    Uma única linha não consegue mais descrever a coleta: o teto de retenção
    recorta a janela por família, então fontes diferentes mediram períodos
    diferentes. O quanto cada uma mediu está na tabela de saúde da coleta.
    """
    base = f"{account.lookback_days} dias"
    if not account.bootstrap:
        return base
    return f"{base} (bootstrap; fonte com retenção menor mediu menos)"


def _collection_health(account: Account) -> tuple[str, str, str, str, str, list[dict]]:
    status = account.collection_status
    status_style = {
        "ok": ("Completa", "#1f6b45", "#e5f3eb"),
        "partial": ("Parcial", "#9a5b10", "#fff0d8"),
        "failed": ("Falhou", "#a72f2f", "#f9e2e2"),
        "not_reported": ("Não informada", "#5b6169", "#eef0ea"),
    }
    label, fg, bg = status_style[status]
    rows = []
    for item in account.collection_health:
        row_label, row_fg, row_bg = {
            "ok": ("OK", "#1f6b45", "#e5f3eb"),
            "partial": ("Parcial", "#9a5b10", "#fff0d8"),
            "unavailable": ("Indisponível", "#a72f2f", "#f9e2e2"),
            "error": ("Erro", "#a72f2f", "#f9e2e2"),
        }.get(item.status, (item.status, "#5b6169", "#eef0ea"))
        rows.append(
            {
                "source": item.source,
                "status": item.status,
                "status_label": row_label,
                "status_fg": row_fg,
                "status_bg": row_bg,
                "required": (
                    "essencial"
                    if item.required
                    else ("cobertura" if item.affects_status else "informativa")
                ),
                "count": (
                    f"{item.collected}/{item.expected}"
                    if item.expected is not None
                    else str(item.collected)
                ),
                "coverage": (
                    f"{item.coverage * 100:.0f}%"
                    if item.coverage is not None
                    else "—"
                ),
                "data_through": item.data_through or "—",
                # A janela é por fonte: o teto de retenção da família recorta o
                # que a conta pediu. Sem esta coluna, ler a cobertura de duas
                # fontes lado a lado compara períodos diferentes.
                "window": f"{item.window_days} dias" if item.window_days else "—",
                "duration": f"{item.duration_ms} ms",
                "result_origin": item.result_origin,
                "origin_label": (
                    f"Cache local ({item.cache_age_seconds}s)"
                    if item.result_origin == "cached"
                    else "AWS nesta execução"
                ),
                "iam_gaps": [
                    {
                        "service": gap.service,
                        "operation": gap.operation,
                        "iam_action": gap.iam_action,
                        "category": gap.category,
                        "affected_resources": gap.affected_resources,
                        "examples": gap.examples,
                    }
                    for gap in item.iam_gaps
                ],
                "iam_actions": ", ".join(
                    sorted({gap.iam_action for gap in item.iam_gaps})
                )
                or "—",
                "iam_affected_resources": sum(
                    gap.affected_resources for gap in item.iam_gaps
                ),
                "error_category": item.error_category or "—",
                "impact": item.impact or "—",
                "next_action": item.next_action or "—",
            }
        )
    if rows:
        ok = sum(item.status == "ok" for item in account.collection_health)
        partial = sum(item.status == "partial" for item in account.collection_health)
        unavailable = sum(
            item.affects_status and item.status in {"unavailable", "error"}
            for item in account.collection_health
        )
        informational = sum(
            not item.affects_status and item.status != "ok"
            for item in account.collection_health
        )
        cached = sum(
            item.result_origin == "cached" for item in account.collection_health
        )
        summary = (
            f"{len(rows)} fontes · {ok} OK · {partial} parciais · "
            f"{unavailable} indisponíveis/erros"
            + (f" · {informational} opcionais desabilitadas" if informational else "")
            + (f" · {cached} via cache local" if cached else "")
        )
    else:
        summary = "Dataset sem telemetria de coleta; cobertura das fontes não informada."
    return status, label, fg, bg, summary, rows


def _athena_views(account: Account) -> tuple[dict, list[dict], list[dict], list[str]]:
    coverage = account.athena_coverage
    if coverage is None:
        return {}, [], [], []
    coverage_vm = {
        "window": f"{coverage.window_start[:10]} → {coverage.window_end[:10]} UTC",
        "workgroups": (
            f"{coverage.workgroups_covered}/{coverage.workgroups_total}"
            if coverage.workgroups_discovery_complete
            else f"{coverage.workgroups_covered}/{coverage.workgroups_total}+ conhecidos"
        ),
        "workgroups_discovery_complete": coverage.workgroups_discovery_complete,
        "configured_workgroups": coverage.configured_workgroups,
        "workgroup_roles": coverage.workgroup_roles,
        "workgroup_operation_status": coverage.workgroup_operation_status,
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
            "reuse_configured": q.reuse_configured_runs,
            "reuse_max_age_minutes": q.reuse_max_age_minutes,
            "reuse_eligible": q.reuse_eligible_runs,
            "reuse_ineligible_reasons": list(q.reuse_ineligible_reasons),
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
            "filter_columns": list(q.filter_columns),
            "partition_candidate_keys": list(q.partition_candidate_keys),
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
    from julius.scoring import months_remaining_in_year

    # Divergência entre cron e execuções, ou cobrança não atribuída, mede a
    # qualidade do inventário — não é economia e não disputa vaga no portfólio.
    integrity = [o for o in opportunities if o.category == "inventory_integrity"]
    opportunities = [o for o in opportunities if o.category != "inventory_integrity"]

    pareto: Pareto = compute_pareto(opportunities)

    technical_identified = sum(
        o.estimated_gain.monthly_expected
        for o in opportunities
        if not o.estimated_gain.is_strategic
    )
    identified = sum(
        o.portfolio_gain.monthly_expected
        for o in opportunities
        if o.include_in_portfolio
    )
    high_conf = sum(
        o.portfolio_gain.monthly_expected
        for o in opportunities
        if o.include_in_portfolio and o.confidence >= _HIGH_CONF
    )
    realizable_year = sum(
        o.portfolio_gain.realizable_year
        for o in opportunities
        if o.include_in_portfolio
    )

    do_now = [o for o in opportunities if o.bucket == "fazer_agora"]
    plan = [o for o in opportunities if o.bucket == "planejar"]
    monitor = [o for o in opportunities if o.bucket == "monitorar"]
    investigate = [o for o in opportunities if o.bucket == "investigar_primeiro"]

    services, _ = _services(account, opportunities)
    attributed_fmt, glue_ce_fmt, delta_fmt, reconciliation_note = _cost_reconciliation(account)
    (
        collection_status,
        collection_status_label,
        collection_status_fg,
        collection_status_bg,
        collection_health_summary,
        collection_health,
    ) = _collection_health(account)
    athena_coverage, athena_queries, athena_actors, athena_gaps = _athena_views(account)

    table_sorted = sorted(opportunities, key=ranking_key, reverse=True)

    account_currency = (
        account.services[0].currency
        if account.services and len({s.currency for s in account.services}) == 1
        else "USD"
    )
    saving_currencies = {
        o.estimation.currency
        for o in opportunities
        if o.estimation is not None and o.include_in_portfolio
    }
    saving_currency = next(iter(saving_currencies), "USD")
    account_saving_comparable = (
        len(saving_currencies) <= 1
        and (not saving_currencies or account_currency in saving_currencies)
    )
    return ReportViewModel(
        account_id=account.account_id,
        account_masked=_mask(account.account_id),
        region=account.region,
        period=account.period,
        lookback=_lookback(account),
        scan_id=manifest_val(manifest, "scan_id"),
        generated_at=account.generated_at,
        currency=account_currency,
        total_cost_fmt=fmt.money(account.billing_cost_mtd, account_currency),
        identified_fmt=fmt.money(identified, saving_currency),
        technical_identified_fmt=fmt.money(
            technical_identified, saving_currency
        ),
        high_conf_fmt=fmt.money(high_conf, saving_currency),
        realizable_year_fmt=fmt.money(realizable_year, saving_currency),
        recommendation=_recommendation(do_now, saving_currency),
        kpi_total=len(opportunities),
        kpi_actionable=len([o for o in opportunities if o.actionable]),
        kpi_strategic=len([o for o in opportunities if o.estimated_gain.is_strategic]),
        pareto_pct=pareto.financial_pct,
        pareto_count=len(pareto.financial_focus),
        pareto_sentence=pareto.sentence,
        pareto_sum_fmt=fmt.money(pareto.financial_sum, saving_currency),
        monthly_total_fmt=fmt.money(pareto.monthly_total, saving_currency),
        pareto_bar=_pareto_bar(pareto, saving_currency),
        executable_pct=pareto.executable_pct,
        executable_count=len(pareto.executable_focus),
        months_remaining=months_remaining_in_year(),
        services=services,
        account_total_fmt=fmt.money(account.billing_cost_mtd, account_currency),
        account_saving_fmt=fmt.money(identified, saving_currency),
        account_saving_pct=(
            f"{round(identified / account.billing_cost_mtd * 100)}%"
            if account.billing_cost_mtd and account_saving_comparable
            else "—"
        ),
        account_bar_w=(
            f"{identified / account.billing_cost_mtd * 100:.1f}%"
            if account.billing_cost_mtd and account_saving_comparable
            else "0%"
        ),
        process_costs=_process_costs(account),
        process_cost_attributed_fmt=attributed_fmt,
        glue_cost_explorer_fmt=glue_ce_fmt,
        process_cost_delta_fmt=delta_fmt,
        process_reconciliation_note=reconciliation_note,
        collection_status=collection_status,
        collection_status_label=collection_status_label,
        collection_status_fg=collection_status_fg,
        collection_status_bg=collection_status_bg,
        collection_health_summary=collection_health_summary,
        collection_health=collection_health,
        rule_families_without_evidence=[
            {
                "service": family.service,
                "name": family.name,
                "requires": ", ".join(family.requires),
                "reason": missing_evidence(account, family),
            }
            for family in families_without_evidence(account)
        ],
        inventory_integrity=[
            {
                "rule_id": o.rule_id,
                "asset": o.asset_name,
                "finding": o.finding,
                "action": o.recommended_action,
                "evidence": list(o.evidence),
            }
            for o in integrity
        ],
        focus=[_opp_vm(o, account_currency) for o in pareto.financial_focus],
        focus_ids=[o.opportunity_id for o in pareto.financial_focus],
        table=[_opp_vm(o, account_currency) for o in table_sorted],
        do_now=[_opp_vm(o, account_currency) for o in do_now],
        plan=[_opp_vm(o, account_currency) for o in plan],
        monitor=[_opp_vm(o, account_currency) for o in monitor],
        investigate=[_opp_vm(o, account_currency) for o in investigate],
        producers=[_producer_vm(p) for p in account.producer_candidates],
        previous_results=[_prev_vm(r) for r in account.previous_results],
        manifest=manifest,
        athena_coverage=athena_coverage,
        athena_queries=athena_queries,
        athena_actors=athena_actors,
        athena_gaps=athena_gaps,
        glue_cost=_glue_cost_view(account),
        s3_cost=_s3_cost_view(account),
    )


def manifest_val(manifest: list[dict], key: str) -> str:
    for item in manifest:
        if item.get("k") == key:
            return str(item.get("v", ""))
    return ""
