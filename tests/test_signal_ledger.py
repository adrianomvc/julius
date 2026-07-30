"""Memória e consequência do veredito da análise contextual."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from julius.config import DEFAULT_CONFIG
from julius.findings.promotion import promote
from julius.findings.signal import Signal
from julius.state import SignalLedger

ACCOUNT = "123456789012"


@dataclass
class _Verdict:
    rule_id: str
    asset_name: str
    verdict: str
    rationale: str


def _signal(**overrides) -> Signal:
    defaults = dict(
        kind="code",
        rule_id="GLUE-CODE-PUSHDOWN",
        asset_type="glue_job",
        asset_name="agrega_vendas",
        observation="Leitura catalogada sem predicate pushdown",
        question="O padrão custa capacidade neste job?",
        missing_evidence=["bytes e partições lidos antes e depois"],
        artifact_sha256="a" * 64,
        lines=[12, 40],
        doc_links=["https://docs.aws.amazon.com/glue/latest/dg/"],
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _record(ledger: SignalLedger, signal: Signal, verdict: str) -> int:
    return ledger.record_verdicts(
        [
            _Verdict(
                rule_id=signal.rule_id,
                asset_name=signal.asset_name,
                verdict=verdict,
                rationale="o volume observado não sustenta o padrão como desperdício",
            )
        ],
        [signal],
        account=ACCOUNT,
        scan_id="scan-1",
        prompt_version="1.3.0",
    )


def test_a_rejected_signal_does_not_come_back_unchanged(tmp_path):
    """Sem isso a análise se gasta re-julgando o que já julgou."""
    ledger = SignalLedger(tmp_path / "signals.json")
    signal = _signal()
    assert _record(ledger, signal, "rejected") == 1

    result = ledger.suppress([signal], ACCOUNT)

    assert result.open == []
    assert result.suppressed == [signal.fingerprint(ACCOUNT)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_sha256", "b" * 64),
        ("lines", [12, 40, 77]),
        ("missing_evidence", []),
    ],
    ids=["script mudou", "linhas mudaram", "evidência que faltava apareceu"],
)
def test_a_rejected_signal_reopens_when_its_evidence_changes(tmp_path, field, value):
    """Um "não" dado sobre uma evidência não é um "não" para sempre."""
    ledger = SignalLedger(tmp_path / "signals.json")
    _record(ledger, _signal(), "rejected")

    changed = _signal(**{field: value})
    result = ledger.suppress([changed], ACCOUNT)

    assert result.open == [changed]
    assert result.reopened == [changed.fingerprint(ACCOUNT)]


def test_needs_evidence_stays_in_the_package(tmp_path):
    """A pergunta segue de pé enquanto a evidência não chegar."""
    ledger = SignalLedger(tmp_path / "signals.json")
    signal = _signal()
    _record(ledger, signal, "needs_evidence")

    assert ledger.suppress([signal], ACCOUNT).open == [signal]


def test_a_confirmed_signal_moves_to_the_investigation_queue(tmp_path):
    """A confirmação fecha a pergunta e abre uma investigação separada."""
    ledger = SignalLedger(tmp_path / "signals.json")
    signal = _signal()
    _record(ledger, signal, "confirmed")

    assert ledger.suppress([signal], ACCOUNT).open == []
    assert ledger.decisions_for(ACCOUNT)[0].status == "candidate"


def test_a_verdict_about_an_unknown_signal_is_ignored(tmp_path):
    """O veredito só vale ancorado no sinal que o pacote realmente enviou."""
    ledger = SignalLedger(tmp_path / "signals.json")

    recorded = ledger.record_verdicts(
        [
            _Verdict(
                rule_id="GLUE-CODE-INVENTADA",
                asset_name="fantasma",
                verdict="rejected",
                rationale="—",
            )
        ],
        [_signal()],
        account=ACCOUNT,
        scan_id="scan-1",
        prompt_version="1.3.0",
    )

    assert recorded == 0


def test_a_confirmed_signal_becomes_a_traceable_finding_without_a_number(tmp_path):
    """A confirmação muda o estado epistêmico, não o financeiro."""
    signal = _signal()
    opportunity = promote(
        signal,
        "o script varre a tabela inteira antes de filtrar",
        account=ACCOUNT,
        config=DEFAULT_CONFIG,
        scan_id="scan-1",
    )

    assert opportunity.origin == "ai_confirmed"
    assert opportunity.blocked is True
    assert opportunity.estimated_gain.monthly_expected == 0
    assert opportunity.estimation is not None
    assert opportunity.estimation.saving_quality == "unavailable"
    # Rastreabilidade: ID estável e a âncora no artefato.
    assert opportunity.opportunity_id
    assert opportunity.fingerprint()
    assert opportunity.evidence_refs[0]["sha256"] == signal.artifact_sha256
    assert opportunity.missing_evidence == signal.missing_evidence


def test_promotion_happens_once(tmp_path):
    """Promover duas vezes duplicaria o achado no backlog."""
    ledger = SignalLedger(tmp_path / "signals.json")
    signal = _signal()
    _record(ledger, signal, "confirmed")

    pending = ledger.pending_promotions(ACCOUNT)
    assert len(pending) == 1

    ledger.mark_promoted([pending[0].fingerprint])
    assert ledger.pending_promotions(ACCOUNT) == []

    # Um novo veredito sobre a mesma evidência não reabre a promoção: dali em
    # diante quem cuida do achado é a reconciliação do backlog.
    _record(ledger, signal, "confirmed")
    assert ledger.pending_promotions(ACCOUNT) == []


def test_the_verdict_records_which_briefing_produced_it(tmp_path):
    """Comparar precisão entre prompts diferentes é comparar perguntas diferentes."""
    ledger = SignalLedger(tmp_path / "signals.json")
    _record(ledger, _signal(), "rejected")

    decision = ledger.decisions_for(ACCOUNT)[0]

    assert decision.prompt_version == "1.3.0"
    assert decision.scan_id == "scan-1"
    assert decision.evidence_hash
    assert decision.rationale


def test_a_corrupt_ledger_does_not_silence_every_signal(tmp_path):
    """Falhar lendo o livro não pode ser lido como "tudo já foi julgado"."""
    path = tmp_path / "signals.json"
    path.write_text("{ nao e json", encoding="utf-8")
    signal = _signal()

    assert SignalLedger(path).suppress([signal], ACCOUNT).open == [signal]


def test_two_scans_the_second_one_asks_less_and_carries_more(tmp_path):
    """O ciclo completo, que é onde o defeito aparecia.

    Scan 1 pergunta; o veredito é gravado; scan 2 não repete o descartado e
    traz o confirmado como achado no portfólio.
    """
    from julius.pipeline import analyze

    sample = "data/sample/consumer-avi.json"
    ledger = SignalLedger(tmp_path / "signals.json")

    first = analyze(sample, ledger=ledger)
    assert first.signals, "o primeiro scan precisa ter o que perguntar"
    rejected, confirmed = first.signals[0], first.signals[-1]
    assert rejected is not confirmed

    ledger.record_verdicts(
        [
            _Verdict(rejected.rule_id, rejected.asset_name, "rejected", "adequado aqui"),
            _Verdict(confirmed.rule_id, confirmed.asset_name, "confirmed", "custa capacidade"),
        ],
        first.signals,
        account=first.account.account_id,
        scan_id=first.scan_id,
        prompt_version="1.3.0",
    )

    second = analyze(sample, ledger=ledger)

    # Nenhum dos dois volta a ser perguntado: um porque foi descartado, o outro
    # porque virou achado e o backlog passou a carregá-lo.
    asked_again = {(s.rule_id, s.asset_name) for s in second.signals}
    assert (rejected.rule_id, rejected.asset_name) not in asked_again
    assert (confirmed.rule_id, confirmed.asset_name) not in asked_again
    assert len(second.signals) == len(first.signals) - 2

    promoted = [o for o in second.opportunities if o.origin == "ai_confirmed"]
    assert promoted == []
    assert len(second.investigations) == 1
    assert second.investigations[0].asset_name == confirmed.asset_name

    # A promoção não pode mexer no dinheiro do portfólio.
    def total(analysis):
        return round(
            sum(o.estimated_gain.monthly_expected for o in analysis.opportunities), 2
        )

    assert total(second) == total(first)

    # E não se repete no terceiro scan.
    third = analyze(sample, ledger=ledger)
    assert len([o for o in third.opportunities if o.origin == "ai_confirmed"]) == 0
    assert len(third.investigations) == 1
