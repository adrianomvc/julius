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
from julius.state import SignalLedger


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
    signals = [Signal(**payload) for payload in context.signals]
    return SignalLedger(ledger_path).record_verdicts(
        analysis.signal_verdicts,
        signals,
        account=analysis.account,
        scan_id=analysis.scan_id,
        prompt_version=context.prompt_version,
    )
