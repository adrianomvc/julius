"""O que o motor ainda não sabe calcular sobre as hipóteses desta conta."""

from __future__ import annotations

import typer

from julius.cli._shared import _DEFAULT_INPUT, app

signals_app = typer.Typer(
    add_completion=False,
    help="Hipóteses da conta: o que já tem cálculo e o que falta.",
)
app.add_typer(signals_app, name="signals")

_ROTULO = {
    "calculated": "com cálculo",
    "candidate": "fórmula existe, falta autorizar",
    "uncovered": "sem cálculo na família",
}


@signals_app.command("coverage")
def signals_coverage(
    input: str = typer.Option(_DEFAULT_INPUT, "--input", "-i"),
    artifacts_manifest: str = typer.Option(
        "",
        "--artifacts-manifest",
        help="Manifesto read-only; sem ele nenhum sinal de código é analisado.",
    ),
    show: str = typer.Option(
        "candidate",
        "--show",
        help="candidate, uncovered, calculated ou all.",
    ),
) -> None:
    """Lista cada hipótese da conta e se alguma fórmula existente já a atende.

    Não acessa a AWS e não altera nada: lê o dataset já coletado e cruza os sinais
    com os métodos autorizados. `candidate` é a linha acionável — a conta já existe
    e falta declarar que ela serve para aquele `rule_id`.
    """
    from julius.knowledge.coverage import coverage_for_signals, summary
    from julius.knowledge.remediation import FAMILIES
    from julius.pipeline import analyze

    if show not in {"candidate", "uncovered", "calculated", "all"}:
        raise typer.BadParameter(
            "--show deve ser candidate, uncovered, calculated ou all"
        )
    try:
        analysis = analyze(input, artifacts_manifest=artifacts_manifest or None)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    linhas = coverage_for_signals(analysis.signals)
    contagem = summary(linhas)
    total = sum(contagem.values())
    typer.echo(
        f"Conta {analysis.account.account_id} · {len(analysis.signals)} sinal(is) · "
        f"{total} regra(s) distinta(s)"
    )
    for estado in ("calculated", "candidate", "uncovered"):
        typer.echo(f"  {contagem[estado]:>3}  {_ROTULO[estado]}")

    escolhidas = [
        linha for linha in linhas if show == "all" or linha.status == show
    ]
    if not escolhidas:
        typer.echo(f"\nNenhuma linha em '{show}'.")
        return

    typer.echo("")
    for linha in escolhidas:
        familia = FAMILIES.get(linha.family)
        typer.echo(
            f"  [{linha.resolved_by:<7} esf {linha.effort}] {linha.rule_id}"
        )
        typer.echo(f"      família: {familia.label if familia else '—'}")
        typer.echo(f"      {linha.reason}")

    if show in {"candidate", "all"} and contagem["candidate"]:
        typer.echo(
            "\nCada 'candidate' vira cálculo acrescentando o rule_id ao mapa em "
            "knowledge/contextual_estimation.py ou knowledge/generative_estimation.py, "
            "com a justificativa que as entradas existentes já carregam."
        )
