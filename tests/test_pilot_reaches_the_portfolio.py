"""O caminho inteiro: sinal → veredito → piloto → dinheiro no portfólio.

`promote()` e `pending_promotions()` existiam e não tinham chamador fora de teste.
Na prática, `julius validate-pilot` gravava o piloto no livro de sinais e o achado
nunca chegava ao portfólio — a peça estava pronta e desligada.

Este arquivo cobre o caminho de ponta a ponta, e principalmente as duas metades que
o piloto exige: **alguém mediu**, e **alguém assinou**. Sem as duas, a promoção
continua sem cifra, que é o comportamento correto e não uma falha.
"""

from __future__ import annotations

from julius.config import DEFAULT_CONFIG
from julius.pipeline import analyze
from julius.state import SignalLedger

SAMPLE = "data/sample/consumer-avi.json"


class _Verdict:
    def __init__(self, rule_id: str, asset_name: str) -> None:
        self.rule_id = rule_id
        self.asset_name = asset_name
        self.verdict = "confirmed"
        self.rationale = "o padrão custa capacidade neste ativo"
        self.recommendation = None
        self.estimation_proposal = None
        self.contextual_estimate = None


def _confirma(tmp_path) -> tuple[SignalLedger, str, str]:
    """Primeiro scan, um sinal confirmado. Devolve o livro e o sinal escolhido."""
    ledger = SignalLedger(tmp_path / "signals.json")
    primeiro = analyze(SAMPLE, ledger=ledger)
    alvo = primeiro.signals[-1]
    ledger.record_verdicts(
        [_Verdict(alvo.rule_id, alvo.asset_name)],
        primeiro.signals,
        account=primeiro.account.account_id,
        scan_id=primeiro.scan_id,
        prompt_version="2.2.0",
    )
    return ledger, alvo.rule_id, alvo.asset_name


def _fingerprint(ledger: SignalLedger, account: str) -> str:
    return ledger.pending_promotions(account)[0].fingerprint


def test_a_signed_pilot_puts_the_finding_in_the_portfolio(tmp_path):
    ledger, rule_id, asset_name = _confirma(tmp_path)
    conta = analyze(SAMPLE).account.account_id
    ledger.record_pilot(
        _fingerprint(ledger, conta),
        actor="adriano",
        measured_monthly=1000.0,
        notes="benchmark A/B no mesmo volume",
    )

    analise = analyze(SAMPLE, ledger=ledger)
    achado = next(o for o in analise.opportunities if o.origin == "ai_confirmed")
    assert achado.rule_id == rule_id
    assert achado.asset_name == asset_name
    assert achado.blocked is False
    assert achado.include_in_portfolio is True


def test_the_contextual_factor_is_what_reaches_the_total(tmp_path):
    """Medido × 0.6, e o fator é mais duro que o determinístico de propósito.

    A razão não é o tamanho do número, é a origem: a oportunidade determinística
    parte de fato medido e o piloto confirma a conta; a contextual parte de leitura
    de código, e o piloto confirma **uma execução**.
    """
    ledger, _, _ = _confirma(tmp_path)
    conta = analyze(SAMPLE).account.account_id
    ledger.record_pilot(
        _fingerprint(ledger, conta), actor="adriano", measured_monthly=1000.0
    )

    analise = analyze(SAMPLE, ledger=ledger)
    achado = next(o for o in analise.opportunities if o.origin == "ai_confirmed")
    esperado = 1000.0 * DEFAULT_CONFIG.contextual_realization_factor
    # O teto por ativo pode reduzir mais, nunca aumentar.
    assert 0 < achado.estimation.estimated_saving <= esperado + 0.01


def test_the_reasoning_survives_into_the_assumptions(tmp_path):
    """Quem lê o número precisa achar de onde ele veio, e quem assinou."""
    ledger, _, _ = _confirma(tmp_path)
    conta = analyze(SAMPLE).account.account_id
    ledger.record_pilot(
        _fingerprint(ledger, conta), actor="adriano", measured_monthly=1000.0
    )

    analise = analyze(SAMPLE, ledger=ledger)
    achado = next(o for o in analise.opportunities if o.origin == "ai_confirmed")
    premissas = " ".join(achado.estimation.assumptions)
    assert "adriano" in premissas
    assert "uma execução" in premissas


def test_without_a_pilot_there_is_no_money(tmp_path):
    ledger, _, _ = _confirma(tmp_path)
    analise = analyze(SAMPLE, ledger=ledger)
    achado = next(o for o in analise.opportunities if o.origin == "ai_confirmed")
    assert achado.blocked is True
    assert achado.estimation.saving_quality == "unavailable"
    assert achado.include_in_portfolio is False


def test_a_promoted_finding_is_never_absorbed_by_a_deterministic_one(tmp_path):
    """Absorvê-lo o transformaria em texto dentro dos riscos de outro achado —
    perdendo o ID, a origem e o ciclo de vida que a promoção existe para dar."""
    ledger, _, asset_name = _confirma(tmp_path)
    analise = analyze(SAMPLE, ledger=ledger)
    promovidos = [o for o in analise.opportunities if o.origin == "ai_confirmed"]
    assert len(promovidos) == 1
    # Existe achado determinístico no mesmo ativo, e ele continua separado.
    mesmos = [o for o in analise.opportunities if o.asset_name == asset_name]
    assert len(mesmos) > 1
    assert all(
        o.origin == "rule" for o in mesmos if o.opportunity_id != promovidos[0].opportunity_id
    )


def test_a_signal_the_rule_stopped_emitting_is_not_promoted(tmp_path):
    """O padrão que sustentava o julgamento não está mais lá."""
    ledger = SignalLedger(tmp_path / "signals.json")
    primeiro = analyze(SAMPLE, ledger=ledger)
    alvo = primeiro.signals[-1]
    ledger.record_verdicts(
        [_Verdict(alvo.rule_id, alvo.asset_name)],
        primeiro.signals,
        account=primeiro.account.account_id,
        scan_id=primeiro.scan_id,
        prompt_version="2.2.0",
    )
    # Uma conta diferente não emite aquele sinal: nada a promover, e nada quebra.
    outra = analyze("data/sample/consumer-nova.json", ledger=ledger)
    assert [o for o in outra.opportunities if o.origin == "ai_confirmed"] == []


def test_the_promotion_happens_once(tmp_path):
    ledger, _, _ = _confirma(tmp_path)
    analyze(SAMPLE, ledger=ledger)
    segundo = analyze(SAMPLE, ledger=ledger)
    assert [o for o in segundo.opportunities if o.origin == "ai_confirmed"] == []
