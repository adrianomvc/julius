"""Geração dos artefatos de relatório."""

from __future__ import annotations

from pathlib import Path

import typer

from julius.cli._shared import (
    _DEFAULT_HISTORY,
    _DEFAULT_INPUT,
    _DEFAULT_PARQUET,
    _DEFAULT_SIGNALS,
    _DEFAULT_STORE,
    _load_agent_enrichment,
    _require_same_opportunity_set,
    app,
)
from julius.collection.normalizers import load_account
from julius.pipeline import analyze
from julius.reporting import excel, renderer
from julius.reporting.contextual import attach_contextual_analysis
from julius.state import BacklogStore, HistoryStore, SignalLedger


@app.command()
def report(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/reports", "--output", "-o"),
    fmt: str = typer.Option(
        "all", "--format", "-f", help="html | json | excel | all"
    ),
    store: str = typer.Option(_DEFAULT_STORE, "--store"),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db"),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
    agent_context: str = typer.Option("", "--agent-context"),
    agent_result: str = typer.Option("", "--agent-result"),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only usado na análise de código."
    ),
    signal_ledger: str = typer.Option(
        _DEFAULT_SIGNALS,
        "--signal-ledger",
        help="Vereditos por sinal: suprime o descartado e promove o confirmado.",
    ),
) -> None:
    """Gera os artefatos (report.html, report.json, report.xlsx, email.html/.txt)."""
    context, contextual = _load_agent_enrichment(agent_context, agent_result)
    if context and str(context.account["id"]) != load_account(input).account_id:
        raise typer.BadParameter("contexto Devin pertence a outra conta.")
    with HistoryStore(history_db) as history:
        a = analyze(
            input,
            store=BacklogStore(store),
            history=history,
            scan_id=context.scan_id if context else None,
            artifacts_manifest=artifacts_manifest or None,
            ledger=SignalLedger(signal_ledger) if signal_ledger else None,
        )
        history.export_parquet(parquet_dir)
    _require_same_opportunity_set(context, a)
    if context:
        attach_contextual_analysis(a.vm, contextual)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    if fmt in ("html", "all"):
        (out / "report.html").write_text(renderer.render_html(a.vm), encoding="utf-8")
        written.append("report.html")
        # A variante de impressão abre os blocos recolhidos: quem exporta em PDF
        # não pode receber uma página com metade do conteúdo dobrado.
        (out / "report-print.html").write_text(
            renderer.render_print_html(a.vm), encoding="utf-8"
        )
        written.append("report-print.html")
    if fmt in ("json", "all"):
        (out / "report.json").write_text(
            renderer.render_json(a.vm, a.opportunities), encoding="utf-8"
        )
        written.append("report.json")
    if fmt in ("excel", "all"):
        excel.write_workbook(a.vm, out / "report.xlsx")
        written.append("report.xlsx")
    if fmt == "all":
        html, text = renderer.render_email(a.vm, report_url="./report.html")
        (out / "email.html").write_text(html, encoding="utf-8")
        (out / "email.txt").write_text(text, encoding="utf-8")
        written += ["email.html", "email.txt"]
    typer.echo(f"Gerado em {out}/: {', '.join(written)}")
    typer.echo(
        f"Saúde da coleta: {a.vm.collection_status_label} · "
        f"{a.vm.collection_health_summary}"
    )
