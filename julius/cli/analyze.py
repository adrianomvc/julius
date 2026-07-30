"""Detecção, priorização e persistência de um scan."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer

from julius.cli._shared import (
    _DEFAULT_HISTORY,
    _DEFAULT_INPUT,
    _DEFAULT_PARQUET,
    _DEFAULT_STORE,
    app,
)
from julius.pipeline import analyze
from julius.portfolio import analyze_portfolio, discover_inputs
from julius.reporting import renderer
from julius.state import BacklogStore, HistoryStore


@app.command()
def opportunities(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i", help="Dataset exportado (JSON)."),
    history_db: str = typer.Option("", "--history-db", help="DuckDB para carregar revisões."),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only de scripts Glue."
    ),
) -> None:
    """Detecta e prioriza oportunidades, imprimindo o ranking."""
    if history_db:
        with HistoryStore(history_db) as history:
            a = analyze(
                input,
                history=history,
                artifacts_manifest=artifacts_manifest or None,
            )
    else:
        a = analyze(input, artifacts_manifest=artifacts_manifest or None)
    typer.echo(f"Conta {a.account.account_id} · {len(a.opportunities)} oportunidades · scan {a.scan_id}")
    typer.echo(
        f"Saúde da coleta: {a.vm.collection_status_label} · "
        f"{a.vm.collection_health_summary}"
    )
    typer.echo(
        f"KPIs: acionabilidade {a.kpis.actionability_rate*100:.0f}% · "
        f"ownership {a.kpis.ownership_rate*100:.0f}% · "
        f"cobertura financeira {a.kpis.coverage_overall*100:.0f}%"
    )
    if a.kpis.precision_at_10 is not None:
        typer.echo(
            f"Revisão Top 10: {a.kpis.reviewed_at_10} itens · "
            f"Precision@10 {a.kpis.precision_at_10*100:.0f}% · "
            f"falsos positivos {a.kpis.false_positives_at_10}"
        )
    typer.echo(f"Recomendação: {a.vm.recommendation}\n")
    typer.echo(f"{'Exec':>4} {'Estrat':>6}  {'Bucket':<20} {'US$/mês':>9}  Oportunidade")
    for o in a.opportunities:
        gain = o.portfolio_gain.monthly_expected
        gain_s = f"{gain:,.2f}" if not o.estimated_gain.is_strategic else "estrat."
        typer.echo(
            f"{o.execution_priority:>4} {o.strategic_priority:>6}  "
            f"{o.bucket:<20} {gain_s:>9}  {o.finding} ({o.asset_name})"
        )


@app.command()
def scan(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/reports", "--output", "-o"),
    store: str = typer.Option(_DEFAULT_STORE, "--store", help="Backlog operacional JSON."),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db", help="Histórico DuckDB."),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only de scripts Glue."
    ),
    cadence: str = typer.Option(
        "",
        "--cadence",
        help="weekly ou monthly; vazio preserva a cadência gravada no dataset.",
    ),
) -> None:
    """Detecta, persiste o histórico e grava report.json."""
    with HistoryStore(history_db) as history:
        a = analyze(
            input,
            store=BacklogStore(store) if store else None,
            history=history,
            artifacts_manifest=artifacts_manifest or None,
            cadence=cadence or None,
        )
        history.export_parquet(parquet_dir)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        renderer.render_json(a.vm, a.opportunities), encoding="utf-8"
    )
    typer.echo(
        f"scan {a.scan_id}: {len(a.opportunities)} oportunidades · "
        f"identificada {a.vm.identified_fmt}/mês -> {out}/report.json"
    )
    typer.echo(
        f"Saúde da coleta: {a.vm.collection_status_label} · "
        f"{a.vm.collection_health_summary}"
    )


@app.command()
def portfolio(
    input_dir: str = typer.Option("data/sample", "--input-dir", help="Pasta com datasets .json."),
    output: str = typer.Option("data/reports", "--output", "-o"),
    store: str = typer.Option(_DEFAULT_STORE, "--store", help="Backlog persistente (histórico)."),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db", help="Histórico DuckDB."),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
    cadence: str = typer.Option(
        "",
        "--cadence",
        help="weekly ou monthly; vazio preserva a cadência dos datasets.",
    ),
) -> None:
    """Roda o Julius em várias contas e agrega o portfólio (multi-conta)."""
    inputs = discover_inputs(input_dir)
    if not inputs:
        raise typer.BadParameter(f"Nenhum dataset .json em {input_dir}")
    with HistoryStore(history_db) as history:
        p = analyze_portfolio(
            inputs,
            store=BacklogStore(store) if store else None,
            history=history,
            cadence=cadence or None,
        )
        history.export_parquet(parquet_dir)

    out = Path(output)
    for a in p.analyses:
        acc_dir = out / a.account.account_id
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "report.html").write_text(renderer.render_html(a.vm), encoding="utf-8")
        (acc_dir / "report.json").write_text(
            renderer.render_json(a.vm, a.opportunities), encoding="utf-8"
        )
        (acc_dir / "process_graph.json").write_text(
            json.dumps(_graph_payload(a), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        html, text = renderer.render_email(a.vm, report_url="./report.html")
        (acc_dir / "email.html").write_text(html, encoding="utf-8")

    index = {
        "total_identified_monthly": p.total_identified_monthly,
        "total_realizable_year": p.total_realizable_year,
        "accounts": [asdict(r) for r in p.rollups],
        "source_coverage": p.source_coverage,
        "rule_coverage": p.rule_coverage,
        "calibration_report": p.calibration_report,
    }
    (out / "portfolio_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    typer.echo(
        f"Portfólio: {len(p.analyses)} contas · identificada total "
        f"US$ {p.total_identified_monthly:,.2f}/mês"
    )
    typer.echo(f"{'Conta':<18} {'Custo/mês':>10} {'Identif./mês':>13} {'Oport.':>6} {'Ação%':>6}")
    for r in p.rollups:
        typer.echo(
            f"{r.account:<18} {r.total_cost_monthly:>10,.0f} {r.identified_monthly:>13,.0f} "
            f"{r.opportunities:>6} {r.actionability_rate*100:>5.0f}%".replace(",", ".")
        )
    typer.echo(f"Relatórios por conta em {out}/<conta>/ · índice: {out}/portfolio_index.json")


@app.command("graph")
def graph_command(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/reports/process_graph.json", "--output", "-o"),
) -> None:
    """Exporta o grafo tipado de processos e dependências."""
    analysis = analyze(input)
    payload = _graph_payload(analysis)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    typer.echo(
        f"Grafo: {len(payload['nodes'])} nós · {len(payload['edges'])} arestas -> {target}"
    )


def _graph_payload(analysis) -> dict:
    return {
        "account": analysis.account.account_id,
        "scan_id": analysis.scan_id,
        "nodes": [
            {
                "id": key.id,
                "account": key.account,
                "kind": key.kind,
                "name": key.name,
                "attributes": asset.attributes,
            }
            for key, asset in sorted(
                analysis.graph.nodes.items(), key=lambda item: item[0]
            )
        ],
        "edges": [
            {
                "source": edge.source.id,
                "target": edge.target.id,
                "type": edge.type.value,
                "evidence": edge.evidence,
                "confidence": edge.confidence,
            }
            for edge in analysis.graph.edges
        ],
    }


if __name__ == "__main__":
    app()


