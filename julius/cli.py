"""CLI do Julius (MVP 1B)."""

from __future__ import annotations

import json
import webbrowser
from dataclasses import asdict
from pathlib import Path

import typer

from julius.notification import NotificationService
from julius.notification.transports import DryRunTransport
from julius.metrics import compute_kpis
from julius.pipeline import analyze
from julius.portfolio import analyze_portfolio, discover_inputs
from julius.report import renderer
from julius.state import BacklogStore, HistoryStore

app = typer.Typer(add_completion=False, help="Julius — oportunidades de otimização AWS (MVP 2).")

_DEFAULT_INPUT = "data/sample/consumer-avi.json"
_DEFAULT_STORE = "data/state/backlog.json"
_DEFAULT_HISTORY = "data/state/julius.duckdb"
_DEFAULT_PARQUET = "data/state/parquet"


@app.command()
def opportunities(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i", help="Dataset exportado (JSON)."),
    history_db: str = typer.Option("", "--history-db", help="DuckDB para carregar revisões."),
) -> None:
    """Detecta e prioriza oportunidades, imprimindo o ranking."""
    if history_db:
        with HistoryStore(history_db) as history:
            a = analyze(input, history=history)
    else:
        a = analyze(input)
    typer.echo(f"Conta {a.account.account_id} · {len(a.opportunities)} oportunidades · scan {a.scan_id}")
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
    typer.echo(f"{'Exec':>4} {'Estrat':>6}  {'Bucket':<20} {'R$/mês':>9}  Oportunidade")
    for o in a.opportunities:
        gain = o.estimated_gain.monthly_expected
        gain_s = f"{gain:,.0f}".replace(",", ".") if not o.estimated_gain.is_strategic else "estrat."
        typer.echo(
            f"{o.execution_priority:>4} {o.strategic_priority:>6}  "
            f"{o.bucket:<20} {gain_s:>9}  {o.finding} ({o.asset_name})"
        )


@app.command()
def report(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/reports", "--output", "-o"),
    fmt: str = typer.Option("all", "--format", "-f", help="html | json | all"),
) -> None:
    """Gera os artefatos (report.html, report.json, email.html/.txt)."""
    a = analyze(input)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    if fmt in ("html", "all"):
        (out / "report.html").write_text(renderer.render_html(a.vm), encoding="utf-8")
        written.append("report.html")
    if fmt in ("json", "all"):
        (out / "report.json").write_text(
            renderer.render_json(a.vm, a.opportunities), encoding="utf-8"
        )
        written.append("report.json")
    if fmt == "all":
        html, text = renderer.render_email(a.vm, report_url="./report.html")
        (out / "email.html").write_text(html, encoding="utf-8")
        (out / "email.txt").write_text(text, encoding="utf-8")
        written += ["email.html", "email.txt"]
    typer.echo(f"Gerado em {out}/: {', '.join(written)}")


@app.command()
def notify(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    mode: str = typer.Option("dry-run", "--mode", help="dry-run (envio ativo entra no MVP 4)."),
    outbox: str = typer.Option("data/outbox", "--outbox"),
    to: str = typer.Option("squad@empresa.com", "--to"),
    report_url: str = typer.Option(
        "", "--report-url", help="URL hospedada do relatório; vazio = anexo (report.html)."
    ),
    open_preview: bool = typer.Option(False, "--open-preview"),
) -> None:
    """Compõe o e-mail e grava a outbox (dry-run por default)."""
    if mode != "dry-run":
        raise typer.BadParameter("Somente --mode dry-run é suportado antes do MVP 4.")
    a = analyze(input)
    # Sem URL hospedada, o relatório completo vai como anexo (delivery_mode=attachment).
    html, text = renderer.render_email(a.vm, report_url=report_url or None)
    report_html = renderer.render_html(a.vm)
    service = NotificationService(DryRunTransport(outbox, a.scan_id))
    result = service.send_report(
        subject=renderer.subject(a.vm),
        sender="julius@empresa.com",
        recipients=[to],
        html_body=html,
        text_body=text,
        scan_id=a.scan_id,
        report_html=report_html,
    )
    typer.echo(f"{result.status} -> {result.outbox_dir}")
    typer.echo(f"Assunto: {renderer.subject(a.vm)}")
    if open_preview and result.outbox_dir:
        webbrowser.open((Path(result.outbox_dir) / "email.html").resolve().as_uri())


@app.command()
def scan(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/reports", "--output", "-o"),
    store: str = typer.Option(_DEFAULT_STORE, "--store", help="Backlog operacional JSON."),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db", help="Histórico DuckDB."),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
) -> None:
    """Detecta, persiste o histórico e grava report.json."""
    with HistoryStore(history_db) as history:
        a = analyze(
            input,
            store=BacklogStore(store) if store else None,
            history=history,
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


@app.command()
def collect(
    profile: str = typer.Option("", "--profile", help="Perfil do AWS CLI (cadeia de credenciais)."),
    region: str = typer.Option("", "--region", help="Região AWS."),
    role_arn: str = typer.Option("", "--role-arn", help="Assume role read-only (multi-conta)."),
    lookback_days: int = typer.Option(90, "--lookback-days"),
    touches_table: str = typer.Option("", "--touches-table", help="Tabela oficial de toques (Athena)."),
    athena_workgroup: str = typer.Option("julius", "--athena-workgroup"),
    athena_output: str = typer.Option("", "--athena-output", help="S3 de resultados do Athena."),
    cloudtrail: bool = typer.Option(
        False, "--cloudtrail", help="Coleta evidências de ator no Event history."
    ),
    datawarm_job: str = typer.Option(
        "", "--datawarm-job", help="Nome/identificador do job publicador DataWarm."
    ),
    output: str = typer.Option("data/collected/account.json", "--output", "-o"),
) -> None:
    """Coleta ao vivo (boto3) e grava um dataset (mesmo schema do exportado)."""
    from julius.aws.collect import collect_account
    from julius.aws.session import assume_role, make_session
    from julius.ingest.dump import account_to_dataset

    session = make_session(profile, region)
    if role_arn:
        session = assume_role(session, role_arn, region or None)
    account = collect_account(
        session,
        lookback_days=lookback_days,
        touches_table=touches_table,
        athena_workgroup=athena_workgroup,
        athena_output=athena_output or None,
        include_cloudtrail=cloudtrail,
        datawarm_job=datawarm_job,
    )

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(account_to_dataset(account), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    typer.echo(
        f"Coletado: conta {account.account_id} · {len(account.glue_jobs)} jobs · "
        f"{len(account.athena_queries)} queries · {len(account.services)} serviços -> {out}"
    )
    typer.echo(f"Rode: julius report --input {out}")


@app.command()
def portfolio(
    input_dir: str = typer.Option("data/sample", "--input-dir", help="Pasta com datasets .json."),
    output: str = typer.Option("data/reports", "--output", "-o"),
    store: str = typer.Option(_DEFAULT_STORE, "--store", help="Backlog persistente (histórico)."),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db", help="Histórico DuckDB."),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
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
    }
    (out / "portfolio_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    typer.echo(
        f"Portfólio: {len(p.analyses)} contas · identificada total "
        f"R$ {p.total_identified_monthly:,.0f}/mês".replace(",", ".")
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
) -> None:
    """Lista ou registra a revisão humana das recomendações do Top 10."""
    analysis = analyze(input)
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
        opportunity = next(
            (item for item in analysis.opportunities if item.opportunity_id == opportunity_id),
            None,
        )
        if opportunity is None:
            raise typer.BadParameter(
                f"Oportunidade {opportunity_id!r} não existe na análise atual."
            )

        history.record_review(
            opportunity,
            is_true_positive=verdict == "confirmed",
            reviewer=reviewer.strip(),
            notes=notes,
        )
        history.export_parquet(parquet_dir)
        labels = history.labels_for(analysis.opportunities)
        kpis = compute_kpis(analysis.account, analysis.opportunities, labels)

    typer.echo(
        f"Revisão registrada: {opportunity.opportunity_id} -> {verdict} · "
        f"Top 10 revisado {kpis.reviewed_at_10}/10"
    )
    if kpis.precision_at_10 is not None:
        typer.echo(
            f"Precision@10 {kpis.precision_at_10*100:.0f}% · "
            f"falsos positivos {kpis.false_positives_at_10} "
            f"({kpis.false_positive_rate_at_10*100:.0f}%)"
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
