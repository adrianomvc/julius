"""Ciclo de vida do backlog: revisão, transição, diff e validação."""

from __future__ import annotations

import typer

from julius.cli._shared import (
    _DEFAULT_HISTORY,
    _DEFAULT_INPUT,
    _DEFAULT_PARQUET,
    _DEFAULT_STORE,
    app,
)
from julius.findings.lifecycle import can_transition
from julius.pipeline import analyze
from julius.reporting import compute_kpis
from julius.state import BacklogStore, HistoryStore, validate_benefit


@app.command()
def review(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    opportunity_id: str = typer.Option(
        "", "--opportunity-id", help="ID exibido pela listagem; vazio lista o Top 10."
    ),
    verdict: str = typer.Option(
        "", "--verdict", help="confirmed | false-positive"
    ),
    reviewer: str = typer.Option("", "--reviewer", help="Nome ou identificador do revisor."),
    notes: str = typer.Option("", "--notes"),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db"),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
    store: str = typer.Option(_DEFAULT_STORE, "--store"),
) -> None:
    """Lista ou registra a revisão humana das recomendações do Top 10."""
    backlog = BacklogStore(store)
    analysis = analyze(input, store=backlog)
    top10 = analysis.opportunities[:10]

    with HistoryStore(history_db) as history:
        if not opportunity_id:
            labels = history.labels_for(top10)
            typer.echo(f"Top 10 para revisão humana — conta {analysis.account.account_id}")
            for position, opportunity in enumerate(top10, start=1):
                label = labels.get(opportunity.opportunity_id)
                state = "confirmada" if label is True else "falso positivo" if label is False else "pendente"
                typer.echo(
                    f"{position:>2}. {opportunity.opportunity_id:<34} "
                    f"{state:<15} {opportunity.asset_name} · {opportunity.rule_id}"
                )
            typer.echo(
                "Registre: julius review --opportunity-id <ID> "
                "--verdict confirmed|false-positive --reviewer <nome>"
            )
            return

        if verdict not in {"confirmed", "false-positive"}:
            raise typer.BadParameter(
                "--verdict deve ser `confirmed` ou `false-positive`."
            )
        if not reviewer.strip():
            raise typer.BadParameter("--reviewer é obrigatório ao registrar uma revisão.")
        selected = next(
            (item for item in analysis.opportunities if item.opportunity_id == opportunity_id),
            None,
        )
        if selected is None:
            raise typer.BadParameter(
                f"Oportunidade {opportunity_id!r} não existe na análise atual."
            )
        opportunity = selected

        history.record_review(
            opportunity,
            is_true_positive=verdict == "confirmed",
            reviewer=reviewer.strip(),
            notes=notes,
        )
        current_status = backlog.status_for(opportunity.fingerprint()) or "detected"
        target_status = "reviewed" if verdict == "confirmed" else "dismissed"
        if can_transition(current_status, target_status):
            lifecycle_event = backlog.transition(
                opportunity.fingerprint(),
                target_status,
                actor=reviewer.strip(),
                reason=notes or f"revisão humana: {verdict}",
            )
            history.record_lifecycle_event(lifecycle_event)
        history.export_parquet(parquet_dir)
        labels = history.labels_for(analysis.opportunities)
        kpis = compute_kpis(analysis.account, analysis.opportunities, labels)

    typer.echo(
        f"Revisão registrada: {opportunity.opportunity_id} -> {verdict} · "
        f"Top 10 revisado {kpis.reviewed_at_10}/10"
    )
    if kpis.precision_at_10 is not None:
        false_positive_rate = kpis.false_positive_rate_at_10 or 0.0
        typer.echo(
            f"Precision@10 {kpis.precision_at_10*100:.0f}% · "
            f"falsos positivos {kpis.false_positives_at_10} "
            f"({false_positive_rate*100:.0f}%)"
        )
    # Um erro de julgamento da IA some na média das regras se as duas camadas
    # forem contadas juntas — e é a comparação entre elas que interessa.
    for origin, precision in sorted(kpis.precision_by_origin.items()):
        origin_label = (
            "regra determinística" if origin == "rule" else "sinal confirmado pela IA"
        )
        typer.echo(
            f"  {origin_label}: precisão {precision*100:.0f}% "
            f"em {kpis.reviewed_by_origin.get(origin, 0)} revisadas"
        )


@app.command("lifecycle")
def lifecycle_command(
    opportunity_id: str = typer.Option(..., "--opportunity-id"),
    status: str = typer.Option(
        ..., "--status", help="reviewed | accepted | planned | implemented | validated | dismissed"
    ),
    actor: str = typer.Option(..., "--actor"),
    reason: str = typer.Option(..., "--reason"),
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    store: str = typer.Option(_DEFAULT_STORE, "--store"),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db"),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
) -> None:
    """Registra uma transição explícita do ciclo de vida."""
    backlog = BacklogStore(store)
    analysis = analyze(input, store=backlog)
    opportunity = _opportunity_by_id(analysis, opportunity_id)
    with HistoryStore(history_db) as history:
        event = backlog.transition(
            opportunity.fingerprint(),
            status,
            actor=actor,
            reason=reason,
        )
        history.record_lifecycle_event(event)
        history.export_parquet(parquet_dir)
    typer.echo(
        f"Estado: {opportunity_id} · {event.from_status} -> {event.to_status}"
    )


@app.command("diff")
def diff_command(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    store: str = typer.Option(_DEFAULT_STORE, "--store"),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db"),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
) -> None:
    """Compara a execução atual com o último snapshot persistido."""
    with HistoryStore(history_db) as history:
        analysis = analyze(
            input,
            store=BacklogStore(store),
            history=history,
        )
        history.export_parquet(parquet_dir)
    if not analysis.events:
        typer.echo("Sem mudanças desde a última execução.")
        return
    typer.echo(f"Diff {analysis.scan_id}: {len(analysis.events)} evento(s)")
    for event in analysis.events:
        values = ""
        if event.previous_value is not None or event.current_value is not None:
            values = f" · {event.previous_value} -> {event.current_value}"
        typer.echo(
            f"- {event.event_type:<18} {event.asset_name} · {event.rule_id}{values}"
        )


@app.command("validate")
def validate_command(
    opportunity_id: str = typer.Option(..., "--opportunity-id"),
    baseline_cost: float = typer.Option(..., "--baseline-cost"),
    after_cost: float = typer.Option(..., "--after-cost"),
    actor: str = typer.Option(..., "--actor"),
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    baseline_volume: float | None = typer.Option(None, "--baseline-volume"),
    after_volume: float | None = typer.Option(None, "--after-volume"),
    baseline_performance: float | None = typer.Option(None, "--baseline-performance"),
    after_performance: float | None = typer.Option(None, "--after-performance"),
    baseline_failure_rate: float | None = typer.Option(None, "--baseline-failure-rate"),
    after_failure_rate: float | None = typer.Option(None, "--after-failure-rate"),
    notes: str = typer.Option("", "--notes"),
    store: str = typer.Option(_DEFAULT_STORE, "--store"),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db"),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
) -> None:
    """Valida o ganho realizado de uma oportunidade implementada."""
    backlog = BacklogStore(store)
    analysis = analyze(input, store=backlog)
    opportunity = _opportunity_by_id(analysis, opportunity_id)
    status = backlog.status_for(opportunity.fingerprint())
    if status != "implemented":
        raise typer.BadParameter(
            f"A oportunidade precisa estar `implemented`; estado atual: {status}."
        )
    result = validate_benefit(
        opportunity,
        baseline_cost=baseline_cost,
        after_cost=after_cost,
        baseline_volume=baseline_volume,
        after_volume=after_volume,
        baseline_performance=baseline_performance,
        after_performance=after_performance,
        baseline_failure_rate=baseline_failure_rate,
        after_failure_rate=after_failure_rate,
        actor=actor,
        notes=notes,
    )
    with HistoryStore(history_db) as history:
        history.record_validation(result)
        event = backlog.transition(
            opportunity.fingerprint(),
            "validated",
            actor=actor,
            reason=notes or "benefício realizado medido",
        )
        history.record_lifecycle_event(event)
        calibration = history.calibration_for(opportunity.rule_id)
        history.export_parquet(parquet_dir)
    typer.echo(
        f"Validada: prevista US$ {result.predicted_monthly:,.2f} · "
        f"realizada US$ {result.realized_monthly:,.2f} · "
        f"precisão {result.estimation_precision*100:.0f}%"
    )
    if calibration is not None:
        typer.echo(
            f"Calibração {opportunity.rule_id}: fator {calibration.factor:.3f} "
            f"({calibration.sample_count} amostras)"
        )


def _opportunity_by_id(analysis, opportunity_id: str):
    opportunity = next(
        (
            item
            for item in analysis.opportunities
            if item.opportunity_id == opportunity_id
        ),
        None,
    )
    if opportunity is None:
        raise typer.BadParameter(
            f"Oportunidade {opportunity_id!r} não existe na análise atual."
        )
    return opportunity


