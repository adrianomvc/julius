"""Atribuição conservadora do custo da janela aos processos do grafo Julius.

O custo por processo é o consumo **realizado** na janela de análise, rateado
entre as raízes que compartilham um ativo. Não há projeção para fim de mês:
extrapolar uma janela parcial era o que fazia o teto de economia de um processo
depender do dia em que o scan rodou.
"""

from __future__ import annotations

from datetime import date

from julius.collection.models import Account, ProcessCost
from julius.config import Config
from julius.estimation.project import build_gain
from julius.graph.ownership import resolve_owner
from julius.opportunities import impact, prioritizer
from julius.opportunities.base import Opportunity


def build_process_costs(
    account: Account, config: Config, *, today: date | None = None
) -> list[ProcessCost]:
    today = today or _account_date(account)
    window_start = account.window_start
    window_end = account.window_end or today.isoformat()
    coverage_days = max(1, account.window_days)
    roots_by_job = _roots_by_job(account)
    rows: dict[tuple[str, str], ProcessCost] = {}

    def row_for(root_type: str, root_name: str) -> ProcessCost:
        return _row(
            rows, account, root_type, root_name, window_start, window_end, coverage_days
        )

    for job in account.glue_jobs:
        roots = roots_by_job.get(job.name) or [("glue_job", job.name)]
        share = 1.0 / len(roots)
        rate = _job_rate(job, config)
        # Sem consumo reportado na janela, o fallback é a modelagem por duração
        # — nunca a projeção de um período maior.
        actual_dpu = job.actual_dpu_hours_window * share
        estimated_dpu = job.estimated_dpu_hours_window * share
        if job.total_dpu_hours_window <= 0:
            estimated_dpu = job.modeled_window_dpu_hours * share
        for root_type, root_name in roots:
            row = row_for(root_type, root_name)
            _inherit_owner(row, account, "glue_job", job.name)
            row.actual_dpu_hours += actual_dpu
            row.estimated_dpu_hours += estimated_dpu
            row.actual_cost_window += actual_dpu * rate
            row.estimated_cost_window += estimated_dpu * rate
            row.allocation_method = (
                "direct" if len(roots) == 1 else "equal_share_across_processes"
            )
            if job.name not in row.component_names:
                row.component_names.append(job.name)

    crawler_roots = _roots_by_crawler(account)
    for crawler in account.glue_crawlers:
        crawler_rate = _rate_from_allocation(
            crawler.allocated_cost, crawler.dpu_hours_window
        ) or config.pricing.glue_dpu_hour
        roots = crawler_roots.get(crawler.name) or [("glue_crawler", crawler.name)]
        share = 1.0 / len(roots)
        for root_type, root_name in roots:
            row = row_for(root_type, root_name)
            _inherit_owner(row, account, "glue_crawler", crawler.name)
            row.actual_dpu_hours += crawler.dpu_hours_window * share
            row.actual_cost_window += crawler.dpu_hours_window * crawler_rate * share
            if crawler.name not in row.component_names:
                row.component_names.append(crawler.name)

    for session in account.interactive_sessions:
        row = row_for("glue_session", session.session_id)
        _inherit_owner(row, account, "glue_session", session.session_id)
        session_rate = _rate_from_allocation(
            session.allocated_cost, session.dpu_hours
        ) or config.pricing.glue_dpu_hour
        row.actual_dpu_hours += session.actual_dpu_hours_window
        row.estimated_dpu_hours += session.estimated_dpu_hours_window
        row.actual_cost_window += session.actual_dpu_hours_window * session_rate
        row.estimated_cost_window += session.estimated_dpu_hours_window * session_rate
        row.component_names.append(session.session_id)

    for brew in account.databrew_jobs:
        row = row_for("databrew_job", brew.name)
        _inherit_owner(row, account, "databrew_job", brew.name)
        databrew_rate = _rate_from_allocation(
            brew.allocated_cost, brew.estimated_node_hours_window
        ) or config.pricing.databrew_node_hour
        row.estimated_cost_window += brew.estimated_node_hours_window * databrew_rate
        row.component_names.append(brew.name)

    for row in rows.values():
        row.currency = config.pricing.currency
        row.actual_cost_window = round(row.actual_cost_window, 2)
        row.estimated_cost_window = round(row.estimated_cost_window, 2)
        row.actual_dpu_hours = round(row.actual_dpu_hours, 4)
        row.estimated_dpu_hours = round(row.estimated_dpu_hours, 4)
        row.component_names.sort()
    return sorted(rows.values(), key=lambda row: row.total_cost_window, reverse=True)


def apply_conservative_caps(
    account: Account,
    opportunities: list[Opportunity],
    config: Config,
    *,
    today: date | None = None,
) -> None:
    """Converte potencial técnico em economia esperada e impede overbooking."""
    factor = max(0.0, min(1.0, config.thresholds.conservative_realization))
    # O teto é o custo do processo expresso por mês, na mesma unidade das
    # economias. Antes era a projeção para fim de mês, que inflava o teto
    # proporcionalmente ao quanto faltava para o mês fechar.
    remaining_by_process = {
        row.process_id: max(0.0, row.monthly_cost) for row in account.process_costs
    }
    ordered = sorted(
        opportunities,
        key=lambda item: (
            not item.blocked,
            item.estimation.estimated_saving if item.estimation else 0.0,
            item.opportunity_id,
        ),
        reverse=True,
    )
    for opportunity in ordered:
        if opportunity.estimation is None or opportunity.estimated_gain.is_strategic:
            continue
        estimation = opportunity.estimation
        if estimation.estimated_saving <= 0:
            continue
        process_rows = _process_rows_for_opportunity(account, opportunity)
        process_cap = (
            sum(remaining_by_process[row.process_id] for row in process_rows)
            if process_rows
            else None
        )
        hard_cap = max(0.0, estimation.baseline_cost)
        if process_cap is not None:
            hard_cap = min(hard_cap, process_cap)
        old_risk = _inferred_risk(opportunity)
        original_expected = max(0.0, estimation.estimated_saving)
        expected = min(
            original_expected * factor,
            hard_cap,
        )
        applied_ratio = (
            expected / original_expected if original_expected > 0 else 0.0
        )
        original_low = (
            estimation.estimated_saving_low
            if estimation.estimated_saving_low is not None
            else opportunity.estimated_gain.monthly_low
        )
        original_high = (
            estimation.estimated_saving_high
            if estimation.estimated_saving_high is not None
            else opportunity.estimated_gain.monthly_high
        )
        low = min(
            expected,
            max(0.0, original_low) * applied_ratio,
        )
        high = min(
            hard_cap,
            max(original_expected, original_high) * applied_ratio,
        )
        high = max(expected, high)
        estimation.estimated_saving = round(expected, 2)
        estimation.estimated_saving_low = round(low, 2)
        estimation.estimated_saving_high = round(high, 2)
        estimation.projected_cost = round(
            max(0.0, estimation.baseline_cost - expected), 2
        )
        assumption = (
            f"economia esperada aplica fator conservador de realização de {factor:.0%}"
        )
        if assumption not in estimation.assumptions:
            estimation.assumptions.append(assumption)
        if process_cap is not None:
            cap_assumption = (
                "economia limitada ao saldo não comprometido dos processos "
                f"relacionados ({process_cap:.2f} {config.pricing.currency})"
            )
            if cap_assumption not in estimation.assumptions:
                estimation.assumptions.append(cap_assumption)
        # Investigações bloqueadas exibem potencial, mas não reservam o saldo
        # financeiro que deve permanecer disponível às ações já confirmadas.
        if not opportunity.blocked:
            _consume_process_cap(remaining_by_process, process_rows, expected)
        opportunity.estimated_gain = build_gain(
            expected,
            opportunity.difficulty_score,
            config,
            monthly_low=estimation.estimated_saving_low,
            monthly_high=estimation.estimated_saving_high,
            today=today,
            maximum=hard_cap,
        )
        opportunity.gain_score = impact.gain_score(expected, config)
        prioritizer.assign(opportunity, risk=old_risk)


def _process_rows_for_opportunity(
    account: Account, opportunity: Opportunity
) -> list[ProcessCost]:
    names = {opportunity.asset_name}
    if opportunity.source_process:
        names.add(opportunity.source_process)
    return [
        row
        for row in account.process_costs
        if row.monthly_cost > 0
        and (
            row.process_name in names
            or any(name in row.component_names for name in names)
        )
    ]


def _consume_process_cap(
    remaining_by_process: dict[str, float],
    rows: list[ProcessCost],
    amount: float,
) -> None:
    remaining = max(0.0, amount)
    for row in sorted(rows, key=lambda item: item.process_id):
        if remaining <= 0:
            break
        available = remaining_by_process[row.process_id]
        consumed = min(available, remaining)
        remaining_by_process[row.process_id] = max(0.0, available - consumed)
        remaining -= consumed


def _inferred_risk(opportunity: Opportunity) -> float:
    denominator = opportunity.gain_score * opportunity.confidence
    if denominator <= 0:
        return 0.6
    return max(
        0.0,
        min(1.2, opportunity.strategic_priority / denominator),
    )


def _roots_by_job(account: Account) -> dict[str, list[tuple[str, str]]]:
    machines_by_name = {machine.name: machine for machine in account.state_machines}
    roots: dict[str, list[tuple[str, str]]] = {}
    for schedule in account.schedules:
        if schedule.target_type != "state_machine":
            continue
        machine = machines_by_name.get(schedule.target_name)
        if machine is None:
            continue
        for job_name in machine.glue_jobs:
            roots.setdefault(job_name, []).append(("schedule", schedule.name))
    for trigger in account.glue_triggers:
        for job_name in trigger.job_names:
            root = ("glue_trigger", trigger.name)
            if root not in roots.setdefault(job_name, []):
                roots[job_name].append(root)
    return roots


def _roots_by_crawler(account: Account) -> dict[str, list[tuple[str, str]]]:
    roots: dict[str, list[tuple[str, str]]] = {}
    for trigger in account.glue_triggers:
        for crawler_name in trigger.crawler_names:
            roots.setdefault(crawler_name, []).append(
                ("glue_trigger", trigger.name)
            )
    return roots


def _row(
    rows: dict[tuple[str, str], ProcessCost],
    account: Account,
    root_type: str,
    root_name: str,
    window_start: str,
    window_end: str,
    coverage_days: int,
) -> ProcessCost:
    key = (root_type, root_name)
    if key not in rows:
        owner = resolve_owner(account, root_type, root_name)
        rows[key] = ProcessCost(
            process_id=f"{root_type}:{root_name}",
            process_name=root_name,
            root_type=root_type,
            owner=owner.owner,
            owner_source=owner.source,
            owner_confidence=owner.confidence,
            owner_event_time=owner.event_time,
            owner_event_name=owner.event_name,
            currency="USD",
            window_start=window_start,
            window_end=window_end,
            window_days=coverage_days,
        )
    return rows[key]


def _inherit_owner(
    row: ProcessCost, account: Account, asset_type: str, asset_name: str
) -> None:
    candidate = resolve_owner(account, asset_type, asset_name)
    newer_equal_confidence = (
        candidate.confidence == row.owner_confidence
        and candidate.event_time
        and candidate.event_time > row.owner_event_time
    )
    if candidate.owner and (
        candidate.confidence > row.owner_confidence or newer_equal_confidence
    ):
        row.owner = candidate.owner
        row.owner_source = candidate.source
        row.owner_confidence = candidate.confidence
        row.owner_event_time = candidate.event_time
        row.owner_event_name = candidate.event_name


def _job_rate(job, config: Config) -> float:
    return _rate_from_allocation(job.allocated_cost, job.total_dpu_hours_window) or (
        config.pricing.glue_ray_mdpu_hour
        if job.capacity_unit == "M-DPU"
        else config.pricing.glue_rate(job.execution_class)
    )


def _rate_from_allocation(allocated: float | None, consumption: float) -> float | None:
    """Tarifa implícita na cobrança rateada; `None` mantém a tarifa de tabela.

    Usar a tarifa observada faz o custo por processo somar exatamente o custo
    alocado do Cost Explorer, sem duplicar a lógica de rateio aqui.
    """
    if allocated is None or consumption <= 0:
        return None
    return allocated / consumption


def _account_date(account: Account) -> date:
    try:
        return date.fromisoformat(account.generated_at[:10])
    except (TypeError, ValueError):
        return date.today()
