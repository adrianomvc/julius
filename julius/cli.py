"""CLI do Julius (MVP 1B)."""

from __future__ import annotations

import json
import os
import webbrowser
from dataclasses import asdict
from pathlib import Path

import typer

from julius.agent import (
    AgentOutputError,
    load_agent_context,
    prepare_agent_workspace,
    validate_result_file,
    write_validated_result,
)
from julius.collection.collectors.glue.scripts import (
    IdentityMismatchError,
    collect_technical_artifacts,
    write_artifact_bundle,
)
from julius.collection.normalizers import load_account
from julius.collection.session import make_session
from julius.collection.targets import (
    AccountTargetError,
    load_account_targets,
    verify_account_targets,
    write_verified_accounts,
)
from julius.config import ANALYSIS_WINDOW_DAYS, DEFAULT_CONFIG
from julius.metrics import compute_kpis
from julius.notification import (
    NotificationPolicy,
    NotificationService,
    RecipientRegistryError,
    SendLog,
    load_recipient_registry,
    load_settings,
)
from julius.notification.transports import DryRunTransport, SmtpTransport
from julius.opportunities.lifecycle import can_transition
from julius.pipeline import analyze
from julius.portfolio import analyze_portfolio, discover_inputs
from julius.report import renderer
from julius.report.contextual import attach_contextual_analysis
from julius.state import BacklogStore, HistoryStore, validate_benefit

app = typer.Typer(add_completion=False, help="Julius — oportunidades de otimização AWS (MVP 2).")
agent_app = typer.Typer(
    add_completion=False,
    help="Ferramentas locais usadas pelo Devin para analisar com o Julius.",
)
app.add_typer(agent_app, name="agent")

_DEFAULT_INPUT = "data/sample/consumer-avi.json"
_DEFAULT_STORE = "data/state/backlog.json"
_DEFAULT_HISTORY = "data/state/julius.duckdb"
_DEFAULT_PARQUET = "data/state/parquet"


def _load_agent_enrichment(
    context_path: str,
    result_path: str,
):
    if bool(context_path) != bool(result_path):
        raise typer.BadParameter(
            "--agent-context e --agent-result devem ser informados juntos."
        )
    if not context_path:
        return None, None
    try:
        context = load_agent_context(context_path)
        contextual = validate_result_file(context_path, result_path)
    except (
        AgentOutputError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(str(exc)) from exc
    return context, contextual


def _require_same_opportunity_set(context, analysis) -> None:
    if context is None:
        return
    expected = {
        str(item["opportunity_id"]) for item in context.opportunities
    }
    available = {
        item.opportunity_id for item in analysis.opportunities
    }
    missing = sorted(expected - available)
    if missing:
        raise typer.BadParameter(
            "o relatório não reproduziu as oportunidades do contexto; "
            "informe o mesmo --artifacts-manifest usado no agent prepare "
            f"(ausentes: {', '.join(missing[:3])})"
        )


@agent_app.command("prepare")
def agent_prepare(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/agent", "--output", "-o"),
    top: int = typer.Option(10, "--top", min=1, max=25),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only de scripts, SQL e ASL."
    ),
) -> None:
    """Prepara contexto, contrato e instruções que o Devin analisará localmente."""
    try:
        analysis = analyze(
            input,
            artifacts_manifest=artifacts_manifest or None,
        )
        context, files = prepare_agent_workspace(
            analysis,
            output,
            top=top,
            artifacts_manifest=artifacts_manifest or None,
        )
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"Contexto Devin preparado: conta {context.account['id']} · "
        f"scan {context.scan_id} · {len(context.opportunities)} oportunidades"
    )
    for path in files:
        typer.echo(f"- {path}")
    typer.echo("Nenhum serviço externo foi chamado.")


@agent_app.command("collect-artifacts")
def agent_collect_artifacts(
    input: str = typer.Option(
        "data/collected/account.json",
        "--input",
        "-i",
        help="Dataset da conta já coletado e verificado.",
    ),
    output: str = typer.Option("data/artifacts", "--output", "-o"),
    sso_profile: str = typer.Option(
        "",
        "--sso-profile",
        help="Nome do perfil SSO no AWS CLI; vazio usa default/AWS_PROFILE.",
    ),
    max_bytes: int = typer.Option(256_000, "--max-bytes", min=1, max=1_000_000),
) -> None:
    """Coleta código/SQL/ASL com o perfil SSO selecionado em sa-east-1."""
    account = load_account(input)
    if account.region != "sa-east-1":
        raise typer.BadParameter(
            f"região da conta deve ser sa-east-1, recebido {account.region}"
        )
    session = make_session(sso_profile or None, "sa-east-1")
    try:
        bundle = collect_technical_artifacts(
            session,
            account,
            max_bytes=max_bytes,
        )
    except (IdentityMismatchError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    manifest = write_artifact_bundle(bundle, output)
    typer.echo(
        f"Artefatos read-only: conta {bundle.account_id} · "
        f"{len(bundle.artifacts)} arquivos · {len(bundle.errors)} falhas"
    )
    typer.echo(f"Manifesto: {manifest}")


@agent_app.command("verify-accounts")
def agent_verify_accounts(
    config: str = typer.Option(
        "~/.julius-accounts.json",
        "--config",
        help="Cadastro da conta esperada para a identidade SSO ativa.",
    ),
    output: str = typer.Option(
        "data/agent/verified-accounts.json",
        "--output",
        "-o",
    ),
) -> None:
    """Valida via STS os perfis SSO habilitados antes da coleta."""
    try:
        targets = load_account_targets(config)
        verified = verify_account_targets(targets)
    except (
        AccountTargetError,
        FileNotFoundError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(str(exc)) from exc
    path = write_verified_accounts(verified, output)
    typer.echo(f"Contas verificadas via STS: {len(verified)}")
    for account in verified:
        typer.echo(
            f"- {account.name}: {account.account_id} · "
            f"região {account.region} · credencial SSO ativa"
        )
    typer.echo(f"Manifesto: {path}")


@agent_app.command("validate")
def agent_validate(
    context: str = typer.Option("data/agent/context.json", "--context"),
    result: str = typer.Option("data/agent/result.json", "--result"),
    output: str = typer.Option("", "--output", "-o"),
) -> None:
    """Valida a análise escrita pelo Devin contra o scan e os guardrails."""
    try:
        analysis = validate_result_file(context, result)
    except (
        AgentOutputError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(str(exc)) from exc
    output_path = (
        Path(output)
        if output
        else Path(result).with_name("validated-result.json")
    )
    write_validated_result(analysis, output_path)
    typer.echo(
        f"Análise Devin válida: conta {analysis.account} · scan {analysis.scan_id} · "
        f"{len(analysis.recommendations)} recomendações"
    )
    typer.echo(f"Resultado validado: {output_path}")


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
        gain = o.estimated_gain.monthly_expected
        gain_s = f"{gain:,.2f}" if not o.estimated_gain.is_strategic else "estrat."
        typer.echo(
            f"{o.execution_priority:>4} {o.strategic_priority:>6}  "
            f"{o.bucket:<20} {gain_s:>9}  {o.finding} ({o.asset_name})"
        )


@app.command()
def report(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/reports", "--output", "-o"),
    fmt: str = typer.Option("all", "--format", "-f", help="html | json | all"),
    store: str = typer.Option(_DEFAULT_STORE, "--store"),
    history_db: str = typer.Option(_DEFAULT_HISTORY, "--history-db"),
    parquet_dir: str = typer.Option(_DEFAULT_PARQUET, "--parquet-dir"),
    agent_context: str = typer.Option("", "--agent-context"),
    agent_result: str = typer.Option("", "--agent-result"),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only usado na análise de código."
    ),
) -> None:
    """Gera os artefatos (report.html, report.json, email.html/.txt)."""
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
    typer.echo(
        f"Saúde da coleta: {a.vm.collection_status_label} · "
        f"{a.vm.collection_health_summary}"
    )


@app.command()
def notify(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    mode: str = typer.Option("dry-run", "--mode", help="dry-run | active"),
    outbox: str = typer.Option("data/outbox", "--outbox"),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only usado na análise de código."
    ),
    to: str = typer.Option(
        "", "--to", help="Destinatários de prévia separados por vírgula (somente dry-run)."
    ),
    report_url: str = typer.Option(
        "", "--report-url", help="URL hospedada do relatório; vazio = anexo (report.html)."
    ),
    open_preview: bool = typer.Option(False, "--open-preview"),
    email_config: str = typer.Option("~/.julius-email.json", "--email-config"),
    recipients_config: str = typer.Option(
        "~/.julius-recipients.json",
        "--recipients-config",
        help="Cadastro local de destinatários por conta.",
    ),
    send_log: str = typer.Option("data/state/send_log.json", "--send-log"),
    recipient_group: str = typer.Option(
        "", "--recipient-group", help="Grupo da prévia (somente dry-run)."
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="Confirmação humana obrigatória para envio ativo manual."
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Somente para grupos previamente aprovados na configuração.",
    ),
    agent_context: str = typer.Option("", "--agent-context"),
    agent_result: str = typer.Option("", "--agent-result"),
) -> None:
    """Compõe em outbox por padrão; envio ativo exige configuração e guardrails."""
    if mode not in {"dry-run", "active"}:
        raise typer.BadParameter("--mode deve ser dry-run ou active.")
    context, contextual = _load_agent_enrichment(agent_context, agent_result)
    if context and str(context.account["id"]) != load_account(input).account_id:
        raise typer.BadParameter("contexto Devin pertence a outra conta.")
    a = analyze(
        input,
        scan_id=context.scan_id if context else None,
        artifacts_manifest=artifacts_manifest or None,
    )
    _require_same_opportunity_set(context, a)
    if context:
        attach_contextual_analysis(a.vm, contextual)
    settings = None
    registered = None
    if mode == "active":
        if to:
            raise typer.BadParameter(
                "--to não é permitido em active; use o cadastro por conta."
            )
        if recipient_group:
            raise typer.BadParameter(
                "--recipient-group não é permitido em active; use o cadastro por conta."
            )
        try:
            settings = load_settings(email_config)
            registered = load_recipient_registry(
                recipients_config
            ).for_account(a.account.account_id)
        except (
            FileNotFoundError,
            RecipientRegistryError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise typer.BadParameter(str(exc)) from exc

    effective_url = report_url or (settings.report_base_url if settings else "")
    html, text = renderer.render_email(a.vm, report_url=effective_url or None)
    attach_report = settings.attach_full_html if settings else True
    report_html = renderer.render_html(a.vm) if attach_report or not effective_url else None
    recipients = [
        address.strip()
        for address in to.split(",")
        if address.strip()
    ]

    if mode == "dry-run":
        recipients = recipients or ["squad@empresa.com"]
        cc: list[str] = []
        effective_group = recipient_group or "account-owners"
        sender = "julius@empresa.com"
        service = NotificationService(DryRunTransport(outbox, a.scan_id))
    else:
        assert settings is not None and registered is not None
        recipients = registered.to
        cc = registered.cc
        effective_group = registered.recipient_group
        sender = settings.sender
        active_transport = SmtpTransport(
            settings.smtp_host,
            port=settings.smtp_port,
            username=os.getenv("JULIUS_SMTP_USERNAME", ""),
            password=os.getenv("JULIUS_SMTP_PASSWORD", ""),
            starttls=settings.smtp_starttls,
        )
        service = NotificationService(
            active_transport,
            policy=NotificationPolicy(settings),
            send_log=SendLog(send_log),
        )

    result = service.send_report(
        subject=renderer.subject(a.vm),
        sender=sender,
        recipients=recipients,
        cc=cc,
        html_body=html,
        text_body=text,
        scan_id=a.scan_id,
        report_html=report_html,
        recipient_group=effective_group,
        mode=mode,
        confirmed=confirm,
        non_interactive=non_interactive,
    )
    destination = result.outbox_dir or result.provider_message_id or result.reason or "-"
    typer.echo(f"{result.status} -> {destination}")
    typer.echo(f"Assunto: {renderer.subject(a.vm)}")
    if result.status == "blocked":
        raise typer.Exit(code=2)
    if open_preview and result.outbox_dir:
        webbrowser.open((Path(result.outbox_dir) / "email.html").resolve().as_uri())


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
) -> None:
    """Detecta, persiste o histórico e grava report.json."""
    with HistoryStore(history_db) as history:
        a = analyze(
            input,
            store=BacklogStore(store) if store else None,
            history=history,
            artifacts_manifest=artifacts_manifest or None,
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
def collect(
    sso_profile: str = typer.Option(
        "",
        "--sso-profile",
        help="Nome do perfil SSO no AWS CLI; vazio usa default/AWS_PROFILE.",
    ),
    lookback_days: int = typer.Option(
        ANALYSIS_WINDOW_DAYS,
        "--lookback-days",
        help="Dias UTC completos da janela de análise (custo e comportamento).",
    ),
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
    """Coleta em sa-east-1 com o perfil SSO selecionado e grava o dataset."""
    from julius.collection.health.recorder import RequiredCollectionError
    from julius.collection.normalizers.dump import account_to_dataset
    from julius.collection.orchestrator import collect_account

    session = make_session(sso_profile or None, "sa-east-1")
    try:
        account = collect_account(
            session,
            config=DEFAULT_CONFIG,
            lookback_days=lookback_days,
            touches_table=touches_table,
            athena_workgroup=athena_workgroup,
            athena_output=athena_output or None,
            include_cloudtrail=cloudtrail,
            datawarm_job=datawarm_job,
        )
    except RequiredCollectionError as exc:
        raise typer.BadParameter(str(exc)) from exc

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
    typer.echo(
        f"Saúde da coleta: {account.collection_status} · "
        f"{len(account.collection_health)} fontes registradas"
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
