"""O piloto é o que separa estimativa validada de economia oficial.

`Maturity.VALIDATED_MODEL` existia e nada chegava lá. Faltavam as duas metades: o
fator que reduz uma cifra nascida de interpretação, e o ato humano que a autoriza.
Sem as duas, o estado era vocabulário sem caminho.

O fator é mais duro que o determinístico — 0.6 contra 0.8 — e a razão não é o
tamanho do número, é a origem. Uma oportunidade determinística parte de fato
medido e o piloto confirma a conta; uma contextual parte de leitura de código, e o
piloto confirma **uma execução**. O que se generaliza dali é menos.
"""

from __future__ import annotations

import pytest

from julius.config import DEFAULT_CONFIG
from julius.findings.promotion import promote
from julius.findings.signal import Signal
from julius.scoring.priority import assign
from julius.state.signal_ledger import PilotResult, SignalLedger


def _sinal() -> Signal:
    return Signal(
        kind="code",
        rule_id="GLUE-CODE-PYTHON-UDF",
        asset_type="glue_job",
        asset_name="etl",
        observation="UDF Python por linha",
        question="é necessária aqui?",
        artifact_sha256="ab" * 32,
        lines=[42],
    )


def _piloto(**overrides) -> PilotResult:
    base = {
        "actor": "adriano",
        "measured_monthly": 1000.0,
        "validated_at": "2026-08-03T10:00:00+00:00",
        "notes": "benchmark A/B no mesmo volume",
    }
    return PilotResult(**{**base, **overrides})


class _Veredito:
    rule_id = "GLUE-CODE-PYTHON-UDF"
    asset_name = "etl"
    verdict = "confirmed"
    rationale = "UDF substituível por função nativa"
    recommendation = None
    estimation_proposal = None
    contextual_estimate = None


def _livro(tmp_path, verdict="confirmed"):
    livro = SignalLedger(tmp_path / "signals.json")
    sinal = _sinal()
    veredito = _Veredito()
    veredito.verdict = verdict
    livro.record_verdicts(
        [veredito], [sinal], account="123", scan_id="s1", prompt_version="2.0.0"
    )
    return livro, sinal.fingerprint("123")


# ---------------------------------------------------------------------------
# O fator
# ---------------------------------------------------------------------------


def test_the_contextual_factor_is_harder_than_the_deterministic_one():
    """0.6 contra 0.8, e a diferença é de procedência, não de tamanho."""
    assert DEFAULT_CONFIG.contextual_realization_factor == 0.6
    assert (
        DEFAULT_CONFIG.contextual_realization_factor
        < DEFAULT_CONFIG.realization_factor
    )


def test_the_pilot_measurement_is_reduced_by_the_factor():
    """O que entra não é o que o piloto mediu — é o que se generaliza dele."""
    achado = promote(
        _sinal(),
        "ok",
        account="123",
        config=DEFAULT_CONFIG,
        scan_id="x",
        pilot=_piloto(measured_monthly=1000.0),
    )

    assert achado.estimation.estimated_saving == 600.0
    assert achado.estimation.baseline_cost == 1000.0


def test_the_reasoning_survives_into_the_assumptions():
    """Quem lê o achado precisa ver quanto foi medido, por quem e com que fator."""
    achado = promote(
        _sinal(),
        "ok",
        account="123",
        config=DEFAULT_CONFIG,
        scan_id="x",
        pilot=_piloto(),
    )
    texto = " ".join(achado.estimation.assumptions)

    assert "1000.00" in texto
    assert "60%" in texto
    assert "adriano" in texto
    assert "uma execução" in texto


# ---------------------------------------------------------------------------
# Sem piloto nada muda
# ---------------------------------------------------------------------------


def test_without_a_pilot_the_promotion_still_carries_no_money():
    """Confirmação contextual dá identidade, não cifra. Continua assim."""
    achado = promote(_sinal(), "ok", account="123", config=DEFAULT_CONFIG, scan_id="x")

    assert achado.estimation.estimated_saving == 0.0
    assert achado.estimation.saving_quality == "unavailable"
    assert achado.include_in_portfolio is False
    assert achado.blocked is True


def test_the_pilot_is_what_unblocks_the_recommendation():
    """Bloqueado enquanto ninguém mediu; medir é exatamente o que desbloqueia."""
    sem = promote(_sinal(), "ok", account="123", config=DEFAULT_CONFIG, scan_id="x")
    com = promote(
        _sinal(),
        "ok",
        account="123",
        config=DEFAULT_CONFIG,
        scan_id="x",
        pilot=_piloto(),
    )

    assert sem.blocked is True
    assert com.blocked is False
    assert "Medir o custo" in sem.recommended_action
    assert "validada pelo piloto" in com.recommended_action


def test_a_piloted_finding_enters_the_portfolio():
    """A trava inteira existe para este momento — e só para ele."""
    achado = promote(
        _sinal(),
        "ok",
        account="123",
        config=DEFAULT_CONFIG,
        scan_id="x",
        pilot=_piloto(),
    )
    achado.owner = "time-dados"
    assign(achado)

    assert achado.include_in_portfolio is True
    assert achado.actionable is True


def test_the_piloted_baseline_does_not_depend_on_the_price_table():
    """O piloto mediu a fatura; nenhuma tarifa foi consultada.

    Exigir tabela verificada aqui bloquearia uma cifra que não veio de tabela
    nenhuma — e a de `sa-east-1` sai do repositório não verificada.
    """
    achado = promote(
        _sinal(),
        "ok",
        account="123",
        config=DEFAULT_CONFIG,
        scan_id="x",
        pilot=_piloto(),
    )

    # `allocated` e não `measured`: o piloto ancora na cobrança real, e
    # `measured` pertence à escala de `saving_quality`. Um valor fora da escala
    # cai no fallback e recebe a pior nota sem ninguém notar —
    # `test_every_quality_vocabulary_value_used_in_the_code_is_mapped` pegou
    # exatamente isso.
    assert achado.estimation.baseline_quality == "allocated"
    # A garantia é que o portão de preço não rebaixa a cifra. `build` ainda
    # deduz a dependência pelo tipo de ativo, e isso é usado noutro lugar — o
    # que decide aqui é `baseline_quality`, e ele diz que o número veio de
    # medição, não de tabela.
    assert achado.estimation.saving_quality == "modeled_evidence"
    assert "cifra bloqueada" not in " ".join(achado.estimation.assumptions)
    assert achado.include_in_portfolio is True


# ---------------------------------------------------------------------------
# Quem assina
# ---------------------------------------------------------------------------


def test_a_pilot_requires_a_confirmed_verdict(tmp_path):
    """Sem veredito antes, o piloto mediria uma hipótese que ninguém leu."""
    livro, fingerprint = _livro(tmp_path, verdict="needs_evidence")

    with pytest.raises(ValueError, match="veredito confirmed"):
        livro.record_pilot(fingerprint, actor="adriano", measured_monthly=100.0)


def test_a_pilot_requires_someone_to_sign(tmp_path):
    """A IA propõe; o humano decide mudança material. `actor` é essa fronteira."""
    livro, fingerprint = _livro(tmp_path)

    with pytest.raises(ValueError, match="quem assina"):
        livro.record_pilot(fingerprint, actor="   ", measured_monthly=100.0)


def test_a_pilot_requires_a_positive_measurement(tmp_path):
    """Piloto que não mediu economia não promove nada."""
    livro, fingerprint = _livro(tmp_path)

    with pytest.raises(ValueError, match="economia positiva"):
        livro.record_pilot(fingerprint, actor="adriano", measured_monthly=0.0)


def test_an_unknown_signal_cannot_be_piloted(tmp_path):
    livro, _ = _livro(tmp_path)

    with pytest.raises(KeyError):
        livro.record_pilot("nao-existe", actor="adriano", measured_monthly=100.0)


def test_the_record_survives_a_reload(tmp_path):
    """O piloto vale entre execuções, ou o scan seguinte perderia a promoção."""
    livro, fingerprint = _livro(tmp_path)
    livro.record_pilot(
        fingerprint, actor="adriano", measured_monthly=1000.0, notes="A/B"
    )

    relido = SignalLedger(tmp_path / "signals.json").decisions_for("123")
    decisao = next(item for item in relido if item.fingerprint == fingerprint)

    assert decisao.pilot is not None
    assert decisao.pilot.actor == "adriano"
    assert decisao.pilot.measured_monthly == 1000.0
    assert decisao.status == "validated_model"


def test_the_command_exists_and_demands_an_actor():
    """Mecanismo sem caminho para acioná-lo é código inalcançável."""
    from julius.cli import app

    nomes = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in app.registered_commands
    }
    assert "validate-pilot" in nomes

    comando = next(
        item for item in app.registered_commands if (item.name or "") == "validate-pilot"
    )
    parametros = set(comando.callback.__annotations__)
    assert {"actor", "measured_monthly", "fingerprint"} <= parametros
