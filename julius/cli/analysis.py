"""Pacote de análise contextual para um provedor preencher."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from julius.analysis import (
    AgentOutputError,
    append_candidates,
    load_agent_context,
    prepare_agent_workspace,
    validate_result_file,
    write_validated_result,
)
from julius.analysis.domain_worker import (
    InboxDomainProvider,
    merge_results,
    process_next,
)
from julius.cli._shared import (
    _DEFAULT_INPUT,
    _DEFAULT_SIGNALS,
    agent_app,
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
from julius.findings.signal import Signal
from julius.pipeline import analyze
from julius.state import RunStore, SignalLedger, file_sha256


@agent_app.command("next")
def agent_next(
    run_store: str = typer.Option(..., "--run-store", help="Ledger DuckDB da fila."),
) -> None:
    """Reserva o próximo pacote imutável para um worker contextual."""
    with RunStore(run_store) as store:
        task = store.claim_next()
        if task is None:
            typer.echo("Nenhum pacote contextual pendente.")
            return
        try:
            checkpoint = store.verified_checkpoint(
                task.account_id,
                task.scan_id,
                task.domain,
            )
        except (KeyError, ValueError) as exc:
            store.transition_task(
                task.task_id,
                "failed",
                error_category="invalid_checkpoint",
            )
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"Job contextual reservado: {task.task_id} · conta {task.account_id} · "
        f"scan {task.scan_id} · domínio {task.domain}"
    )
    typer.echo(f"Payload verificado: {checkpoint.payload_path}")
    typer.echo(f"Hash: {checkpoint.payload_hash}")
    typer.echo("Nenhum cliente boto3 foi entregue ao worker.")


@agent_app.command("work-domains")
def agent_work_domains(
    run_store: str = typer.Option(..., "--run-store", help="Ledger DuckDB da fila."),
    inbox: str = typer.Option(..., "--inbox", help="Respostas por conta/scan/hash."),
    output: str = typer.Option(
        "data/state/contextual-results",
        "--output",
        "-o",
        help="Resultados validados e imutáveis.",
    ),
    provider_name: str = typer.Option("file", "--provider-name"),
    max_jobs: int = typer.Option(10, "--max-jobs", min=1, max=1_000),
    recover_running: bool = typer.Option(
        False,
        "--recover-running",
        help="Recoloca jobs abandonados em pending antes de iniciar.",
    ),
) -> None:
    """Processa pacotes de domínio sem sessão ou cliente boto3."""
    provider = InboxDomainProvider(inbox, name=provider_name)
    with RunStore(run_store) as store:
        before = store.queue_stats()
        typer.echo(
            "Fila IA antes: "
            f"{before.pending} pending · {before.running} running · "
            f"espera máxima {before.oldest_pending_ms / 1000:.1f}s"
        )
        if recover_running:
            recovered = store.requeue_running()
            typer.echo(f"Jobs abandonados recuperados: {recovered}")
        processed = 0
        for _ in range(max_jobs):
            outcome = process_next(store, provider, output)
            if outcome is None:
                break
            if outcome.status == "pending":
                typer.echo(f"Resposta ainda ausente: {outcome.result_path}")
                break
            processed += 1
            typer.echo(
                f"Job {outcome.task_id}: {outcome.status}"
                + (f" · {outcome.result_path}" if outcome.result_path else "")
                + (
                    f" · {outcome.error_category}"
                    if outcome.error_category
                    else ""
                )
            )
        after = store.queue_stats()
        typer.echo(
            "Fila IA depois: "
            f"{after.pending} pending · {after.running} running · "
            f"{after.failed} failed · {after.rejected} rejeitado(s)"
        )
    typer.echo(f"Jobs processados: {processed}. Nenhum cliente boto3 foi criado.")


@agent_app.command("merge-domains")
def agent_merge_domains(
    run_store: str = typer.Option(..., "--run-store", help="Ledger DuckDB da fila."),
    account_id: str = typer.Option(..., "--account-id"),
    scan_id: str = typer.Option(..., "--scan-id"),
    output: str = typer.Option(..., "--output", "-o"),
) -> None:
    """Compõe anexos contextuais validados sem regravar o dataset oficial."""
    try:
        with RunStore(run_store) as store:
            path = merge_results(store, account_id, scan_id, output)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Contexto por domínio mesclado: {path}")
    typer.echo("O dataset determinístico não foi alterado.")


@agent_app.command("prepare")
def agent_prepare(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    output: str = typer.Option("data/agent", "--output", "-o"),
    top: int = typer.Option(10, "--top", min=1, max=25),
    artifacts_manifest: str = typer.Option(
        "", "--artifacts-manifest", help="Manifesto read-only de scripts, SQL e ASL."
    ),
    run_store: str = typer.Option(
        "",
        "--run-store",
        help="Ledger DuckDB opcional para desacoplar a análise contextual.",
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
    if run_store:
        try:
            job_id = _register_agent_job(context, files, run_store)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"Análise contextual enfileirada: {job_id}")
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
        verified, falhas = verify_account_targets(targets)
    except (
        AccountTargetError,
        FileNotFoundError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(str(exc)) from exc
    path = write_verified_accounts(verified, output)
    typer.echo(f"Contas verificadas via STS: {len(verified)} de {len(targets)}")
    for account in verified:
        typer.echo(
            f"- {account.name}: {account.account_id} · "
            f"região {account.region} · credencial SSO ativa"
        )
    # Um perfil que não respondeu não some do relato: sem isto, "verifiquei 3 de
    # 5" viraria "verifiquei 3" e ninguém iria renovar o login das outras duas.
    for falha in falhas:
        typer.echo(f"- {falha}: não verificada — renovar o login SSO deste perfil")
    typer.echo(f"Manifesto: {path}")


@agent_app.command("validate")
def agent_validate(
    context: str = typer.Option("data/agent/context.json", "--context"),
    result: str = typer.Option("data/agent/result.json", "--result"),
    output: str = typer.Option("", "--output", "-o"),
    rule_candidates: str = typer.Option(
        "data/state/rule-candidates.json",
        "--rule-candidates",
        help="Fila de padrões fora do catálogo, acumulada entre scans e contas.",
    ),
    signal_ledger: str = typer.Option(
        _DEFAULT_SIGNALS,
        "--signal-ledger",
        help="Vereditos por sinal: evita re-julgar o que já foi julgado.",
    ),
    run_store: str = typer.Option(
        "",
        "--run-store",
        help="Ledger DuckDB usado no prepare; conclui o job do mesmo contexto.",
    ),
) -> None:
    """Valida a análise escrita pelo Devin contra o scan e os guardrails."""
    try:
        analysis = validate_result_file(context, result)
        registered = (
            append_candidates(analysis, rule_candidates) if rule_candidates else 0
        )
        judged = _record_verdicts(context, analysis, signal_ledger)
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
    if run_store:
        try:
            _complete_agent_job(context, run_store)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    confirmed = sum(1 for v in analysis.signal_verdicts if v.verdict == "confirmed")
    typer.echo(
        f"Análise Devin válida: conta {analysis.account} · scan {analysis.scan_id} · "
        f"{len(analysis.recommendations)} recomendações"
    )
    typer.echo(
        f"Sinais julgados: {len(analysis.signal_verdicts)} "
        f"({confirmed} confirmados) · achados fora do catálogo: {registered}"
    )
    if judged:
        typer.echo(
            f"Vereditos gravados em {signal_ledger}: os descartados não voltam "
            "até a evidência mudar."
        )
    if registered:
        typer.echo(f"Candidatos a nova regra: {rule_candidates}")
    typer.echo(f"Resultado validado: {output_path}")


def _register_agent_job(context, files: list[Path], store_path: str) -> str:
    context_path = next(path for path in files if path.name == "context.json")
    context_hash = file_sha256(context_path)
    health = context.constraints.get("collection_health", [])
    sources = {
        str(item.get("source", "")): str(item.get("status", "unknown"))
        for item in health
        if item.get("source")
    }
    collection_status = context.constraints.get("collection_status", "not_reported")
    checkpoint_status = {
        "ok": "ready",
        "partial": "partial",
        "failed": "unavailable",
    }.get(collection_status, "partial")
    with RunStore(store_path) as store:
        store.create_run(
            context.account["id"],
            context.scan_id,
            status="deterministic_published",
            deterministic_path=str(context_path),
        )
        store.checkpoint(
            context.account["id"],
            context.scan_id,
            "cross_service",
            status=checkpoint_status,
            payload_path=str(context_path),
            payload_hash=context_hash,
            sources=sources,
        )
        return store.enqueue_ai(
            context.account["id"],
            context.scan_id,
            "cross_service",
            context_hash=context_hash,
            payload_path=str(context_path),
        )


def _complete_agent_job(context_path: str, store_path: str) -> None:
    context = load_agent_context(context_path)
    context_hash = file_sha256(context_path)
    with RunStore(store_path) as store:
        task = store.task_for_context(
            context.account["id"],
            context.scan_id,
            "cross_service",
            context_hash,
        )
        if task.status == "failed":
            store.transition_task(task.task_id, "pending")
            task = store.task_for_context(
                context.account["id"],
                context.scan_id,
                "cross_service",
                context_hash,
            )
        if task.status == "pending":
            store.transition_task(task.task_id, "running")
        store.complete_ai(task.task_id)


def _record_verdicts(context_path: str, analysis, ledger_path: str) -> int:
    """Reconstrói os sinais do pacote para ancorar cada veredito na evidência.

    O veredito chega sem a assinatura de evidência — a IA responde sobre o que
    leu, não sobre como o Julius identifica aquilo. Reidratar o sinal a partir
    do contexto é o que permite dizer, na próxima execução, se o que sustentava
    o descarte continua igual.
    """
    if not ledger_path:
        return 0
    context = load_agent_context(context_path)
    signals = [Signal.from_dict(payload) for payload in context.signals]
    return SignalLedger(ledger_path).record_verdicts(
        analysis.signal_verdicts,
        signals,
        account=analysis.account,
        scan_id=analysis.scan_id,
        prompt_version=context.prompt_version,
    )
