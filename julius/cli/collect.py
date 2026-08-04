"""Coleta ao vivo de uma conta AWS."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from julius.cli._shared import (
    app,
)
from julius.collection.bootstrap import resolve_depth
from julius.collection.session import make_session
from julius.config import ANALYSIS_WINDOW_DAYS, DEFAULT_CONFIG


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
    athena_history_workgroups: str = typer.Option(
        "",
        "--athena-history-workgroups",
        help=(
            "Fallback separado por vírgula quando athena:ListWorkGroups for negado; "
            "a cobertura permanece parcial."
        ),
    ),
    athena_output: str = typer.Option("", "--athena-output", help="S3 de resultados do Athena."),
    cloudtrail: bool = typer.Option(
        False, "--cloudtrail", help="Coleta evidências de ator no Event history."
    ),
    datawarm_job: str = typer.Option(
        "", "--datawarm-job", help="Nome/identificador do job publicador DataWarm."
    ),
    account_name: str = typer.Option(
        "",
        "--account-name",
        help=(
            "Nome lógico da conta. Restringe o Glue Catalog; vazio resolve pelo "
            "cadastro de contas e depois pelo --sso-profile."
        ),
    ),
    cadence: str = typer.Option(
        "weekly",
        "--cadence",
        help="weekly usa 30 dias móveis; monthly usa um mês-calendário completo.",
    ),
    period: str = typer.Option(
        "",
        "--period",
        help="Mês YYYY-MM para --cadence monthly; vazio usa o último mês completo.",
    ),
    accounts_config: str = typer.Option(
        "~/.julius-accounts.json",
        "--accounts-config",
        help="Cadastro que relaciona nome lógico, Account ID e perfil SSO.",
    ),
    scope_profile: str = typer.Option(
        "",
        "--scope-profile",
        help=(
            "Perfil de escopo: consumer_datamesh ou full_analysis. "
            "Vazio usa o cadastro; sem cadastro preserva full_analysis."
        ),
    ),
    max_scan_cost: float | None = typer.Option(
        None,
        "--max-scan-cost",
        min=0.0,
        help=(
            "Orçamento estimado (soft limit) em USD; impede novas fontes "
            "opcionais, mas uma fonte iniciada pode ultrapassá-lo."
        ),
    ),
    glue_databases: str = typer.Option(
        "",
        "--glue-databases",
        help="Bancos do catálogo, separados por vírgula. Substitui a regra de nome.",
    ),
    s3_full_listing: bool = typer.Option(
        False,
        "--s3-full-listing",
        help=(
            "Lista cada prefixo S3 até o fim, em paralelo, em vez de parar no "
            "teto de páginas. Necessário para recomendar classe de "
            "armazenamento sobre volume medido; cobra requests de LIST, e o "
            "total gasto aparece na saúde da coleta."
        ),
    ),
    s3_inventory: bool = typer.Option(
        False,
        "--s3-inventory",
        help=(
            "Consome S3 Inventory CSV já configurado para acelerar prefixos; "
            "manifesto ausente ou incompatível mantém o fallback atual."
        ),
    ),
    sagemaker_full_metrics: bool = typer.Option(
        False,
        "--sagemaker-full-metrics",
        help=(
            "Coleta métricas CloudWatch detalhadas de todos os jobs SageMaker; "
            "por padrão detalha os 100 de maior custo potencial."
        ),
    ),
    collection_execution: str = typer.Option(
        "parallel",
        "--collection-execution",
        help=(
            "parallel usa o DAG com limites por serviço; serial é o caminho "
            "conservador de equivalência e rollback."
        ),
    ),
    snapshot_dir: str = typer.Option(
        "",
        "--snapshot-dir",
        help=(
            "Cache local opt-in para fontes elegíveis; isolado por conta, região, "
            "escopo, versão e TTL. Vazio sempre coleta da AWS."
        ),
    ),
    run_store: str = typer.Option(
        "",
        "--run-store",
        help=(
            "Ledger DuckDB opcional; fecha checkpoints por domínio e libera "
            "pacotes imutáveis sem esperar o restante da coleta."
        ),
    ),
    resume_scan_id: str = typer.Option(
        "",
        "--resume-scan-id",
        help=(
            "Retoma domínios ready do mesmo scan quando hash, janela, escopo "
            "e opções coincidirem; requer --run-store."
        ),
    ),
    checkpoint_dir: str = typer.Option(
        "",
        "--checkpoint-dir",
        help="Diretório dos payloads; vazio usa um diretório ao lado do RunStore.",
    ),
    enqueue_domain_ai: bool = typer.Option(
        True,
        "--enqueue-domain-ai/--no-enqueue-domain-ai",
        help="Enfileira cada checkpoint fechado para processamento contextual.",
    ),
    denied_iam_actions: str = typer.Option(
        "",
        "--denied-iam-actions",
        help=(
            "Ações read-only comprovadamente negadas, separadas por vírgula. "
            "Evita chamadas de rede somente por declaração explícita deste scan."
        ),
    ),
    output: str = typer.Option("data/collected/account.json", "--output", "-o"),
    bootstrap: bool | None = typer.Option(
        None,
        "--bootstrap/--no-bootstrap",
        help=(
            "Janela profunda na primeira coleta da conta. Sem a flag, decide "
            "pela existência do --output anterior. Ignorado em --cadence monthly."
        ),
    ),
) -> None:
    """Coleta em sa-east-1 com o perfil SSO selecionado e grava o dataset."""
    from julius.collection.health.recorder import RequiredCollectionError
    from julius.collection.normalizers.dump import account_to_dataset
    from julius.collection.orchestrator import collect_account
    from julius.collection.policy import policy_for_profile
    from julius.collection.sagemaker_history import carry_consistent_scans
    from julius.collection.scope import CatalogScope
    from julius.collection.snapshot import CollectionSnapshotStore
    from julius.collection.targets import (
        resolve_account_name,
        resolve_athena_workgroup_roles,
        resolve_athena_workgroups,
        resolve_scope_profile,
    )
    from julius.collection.window import AnalysisWindow
    from julius.state import RunStore
    from julius.state.audit import new_scan_id

    if cadence not in {"weekly", "monthly"}:
        raise typer.BadParameter("--cadence deve ser weekly ou monthly")
    if collection_execution not in {"parallel", "serial"}:
        raise typer.BadParameter(
            "--collection-execution deve ser parallel ou serial"
        )
    if resume_scan_id and not run_store:
        raise typer.BadParameter("--resume-scan-id requer --run-store")
    if bootstrap and cadence == "monthly":
        raise typer.BadParameter(
            "--bootstrap não se aplica a --cadence monthly: o mês-calendário é "
            "fechamento financeiro de um período específico, não janela móvel"
        )
    dias, usar_bootstrap = resolve_depth(
        lookback_days=lookback_days,
        cadence=cadence,
        checkpoint=output,
        explicit=bootstrap,
    )
    try:
        analysis_window = (
            AnalysisWindow.calendar_month(period)
            if cadence == "monthly" and period
            else AnalysisWindow.previous_calendar_month()
            if cadence == "monthly"
            else AnalysisWindow.trailing(days=dias)
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    session = make_session(sso_profile or None, "sa-east-1")
    resolved_account_name = resolve_account_name(
        explicit_name=account_name,
        sso_profile=sso_profile,
        config_path=accounts_config,
    )
    scope = CatalogScope(
        account_name=resolved_account_name,
        databases=tuple(
            part.strip() for part in glue_databases.split(",") if part.strip()
        ),
    )
    try:
        resolved_athena_workgroups = resolve_athena_workgroups(
            explicit_names=athena_history_workgroups,
            sso_profile=sso_profile,
            config_path=accounts_config,
        )
        resolved_athena_roles = resolve_athena_workgroup_roles(
            sso_profile=sso_profile,
            config_path=accounts_config,
        )
        policy = policy_for_profile(
            resolve_scope_profile(
                explicit_profile=scope_profile,
                sso_profile=sso_profile,
                config_path=accounts_config,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    pipeline_store = RunStore(run_store) if run_store else None
    scan_id = ""
    if pipeline_store is not None:
        scan_id = resume_scan_id or new_scan_id()
    resolved_checkpoint_dir = (
        Path(checkpoint_dir)
        if checkpoint_dir
        else Path(run_store).with_suffix("") / "checkpoints"
        if run_store
        else None
    )
    try:
        account = collect_account(
            session,
            config=DEFAULT_CONFIG,
            lookback_days=lookback_days,
            touches_table=touches_table,
            athena_workgroup=athena_workgroup,
            athena_history_workgroups=resolved_athena_workgroups,
            athena_workgroup_roles=resolved_athena_roles,
            athena_output=athena_output or None,
            include_cloudtrail=cloudtrail,
            datawarm_job=datawarm_job,
            catalog_scope=scope,
            s3_full_listing=s3_full_listing,
            s3_inventory=s3_inventory,
            sagemaker_full_metrics=sagemaker_full_metrics,
            scope_policy=policy,
            max_scan_cost_usd=max_scan_cost,
            window=analysis_window,
            cadence=cadence,
            bootstrap=usar_bootstrap,
            collection_execution=collection_execution,
            snapshot_store=(
                CollectionSnapshotStore(snapshot_dir) if snapshot_dir else None
            ),
            scan_id=scan_id,
            run_store=pipeline_store,
            checkpoint_dir=resolved_checkpoint_dir,
            enqueue_domain_ai=enqueue_domain_ai,
            denied_iam_actions=frozenset(
                action.strip()
                for action in denied_iam_actions.split(",")
                if action.strip()
            ),
            resume_checkpoints=bool(resume_scan_id),
        )
    except RequiredCollectionError as exc:
        if pipeline_store is not None:
            pipeline_store.close()
        raise typer.BadParameter(str(exc)) from exc
    except Exception:
        if pipeline_store is not None:
            pipeline_store.close()
        raise

    out = Path(output)
    try:
        carry_consistent_scans(account, out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                account_to_dataset(account),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        if pipeline_store is not None:
            pipeline_store.publish_deterministic(account.account_id, scan_id, str(out))
            checkpoints = pipeline_store.checkpoints(account.account_id, scan_id)
            typer.echo(
                f"Checkpoints: scan {scan_id} · {len(checkpoints)} domínio(s) · "
                f"ledger {run_store}"
            )
    finally:
        if pipeline_store is not None:
            pipeline_store.close()
    typer.echo(
        f"Coletado: conta {account.account_id} · {len(account.glue_jobs)} jobs · "
        f"{len(account.athena_queries)} queries · {len(account.services)} serviços -> {out}"
    )
    typer.echo(
        f"Saúde da coleta: {account.collection_status} · "
        f"{len(account.collection_health)} fontes registradas · "
        f"escopo {account.scope_profile}"
    )
    typer.echo(
        f"Execução: {account.run_telemetry.execution_mode} · "
        f"{account.run_telemetry.collection_wall_ms / 1000:.1f}s de parede · "
        f"pico {account.run_telemetry.max_parallel_sources} fonte(s)"
    )
    if snapshot_dir:
        typer.echo(
            f"Snapshots: {account.run_telemetry.snapshot_hits} hit(s) · "
            f"{account.run_telemetry.snapshot_misses} miss(es)"
        )
    if account.run_telemetry.cloudwatch_metric_requests:
        typer.echo(
            "CloudWatch: "
            f"{account.run_telemetry.cloudwatch_metric_queries} consulta(s) · "
            f"{account.run_telemetry.cloudwatch_metric_batches} lote(s) · "
            f"{account.run_telemetry.cloudwatch_coalesced_requests} "
            "requisição(ões) agrupada(s)"
        )
    if account.run_telemetry.iam_short_circuits:
        typer.echo(
            f"IAM: {account.run_telemetry.iam_short_circuits} chamada(s) "
            "evitada(s) por manifesto explícito"
        )
    for line in slowest_sources(account.collection_health):
        typer.echo(line)
    typer.echo(f"Rode: julius report --input {out}")


def slowest_sources(health: list, limit: int = 5) -> list[str]:
    """As fontes mais lentas da coleta, para quem for otimizá-la.

    O tempo por fonte já era medido e gravado (`CollectionHealth.duration_ms`);
    ele só não era mostrado a ninguém. Sem isto, decidir onde otimizar a coleta
    depende de ler o JSON — e na prática vira palpite sobre qual chamada AWS
    está cara.
    """
    ranked = sorted(
        (entry for entry in health if entry.duration_ms > 0),
        key=lambda entry: entry.duration_ms,
        reverse=True,
    )[:limit]
    if not ranked:
        return []
    total = sum(entry.duration_ms for entry in health)
    lines = [f"Tempo por fonte (total {total / 1000:.1f}s), as mais lentas:"]
    lines.extend(
        f"  {entry.duration_ms / 1000:6.1f}s  {entry.source}"
        + (f" · {entry.collected} itens" if entry.collected else "")
        for entry in ranked
    )
    return lines
