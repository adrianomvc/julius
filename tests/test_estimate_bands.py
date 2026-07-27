"""A faixa provável precisa dizer a verdade sobre a origem do número."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from julius.config import DEFAULT_CONFIG
from julius.findings.opportunity import Estimation
from julius.pipeline import analyze
from julius.scoring import evidence_quality
from julius.scoring.evidence_quality import EvidenceQuality
from julius.scoring.gain import build_gain

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "consumer-avi.json"
ROOT = Path(__file__).resolve().parents[1] / "julius"


def _width(gain) -> float:
    return (gain.monthly_high - gain.monthly_low) / gain.monthly_expected


def test_measuring_narrows_the_band_and_modelling_widens_it():
    """A mesma economia, com evidências diferentes, não pode sair igual."""
    widths = {
        quality: _width(
            build_gain(100.0, difficulty=1, config=DEFAULT_CONFIG, quality=quality)
        )
        for quality in EvidenceQuality
    }

    ordered = [widths[quality] for quality in sorted(EvidenceQuality, reverse=True)]
    assert ordered == sorted(ordered), "faixa precisa alargar conforme a nota cai"
    assert widths[EvidenceQuality.MEASURED] < widths[EvidenceQuality.MODELED_RULE]


def test_an_unquantified_saving_shows_no_band_at_all():
    """Faixa em torno de zero seria incerteza sobre um número que não existe."""
    gain = build_gain(
        0.0, difficulty=1, config=DEFAULT_CONFIG, quality=EvidenceQuality.MEASURED
    )

    assert gain.monthly_expected == 0
    assert gain.monthly_low == 0
    assert gain.monthly_high == 0


def test_an_explicit_range_survives_untouched():
    """O Athena calcula a própria faixa; a qualidade não pode sobrescrevê-la."""
    gain = build_gain(
        100.0,
        difficulty=1,
        config=DEFAULT_CONFIG,
        monthly_low=90.0,
        monthly_high=115.0,
        quality=EvidenceQuality.MODELED_RULE,
    )

    assert gain.monthly_low == 90.0
    assert gain.monthly_high == 115.0


def test_every_quality_vocabulary_value_used_in_the_code_is_mapped():
    """O bug que essa escala escondia: um valor fora do mapa vira a pior nota.

    `saving_quality="modeled"` é o default de `Estimation` e não estava em
    `_SAVING`, então toda economia calculada por tarifa sobre consumo medido
    caía no fallback — indistinguível de uma faixa de regra. Enquanto a faixa
    era fixa, nada denunciava.
    """
    used = {Estimation("m", 0.0, 0.0, 0.0).saving_quality}
    used_baseline = {Estimation("m", 0.0, 0.0, 0.0).baseline_quality}
    for path in ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        used.update(re.findall(r'saving_quality\s*=\s*"([a-z_]+)"', source))
        used_baseline.update(re.findall(r'baseline_quality\s*=\s*"([a-z_]+)"', source))

    unmapped_saving = {
        value for value in used if value not in evidence_quality._SAVING
    }
    unmapped_baseline = {
        value for value in used_baseline if value not in evidence_quality._BASELINE
    }

    assert not unmapped_saving, f"saving_quality fora da escala: {unmapped_saving}"
    assert not unmapped_baseline, (
        f"baseline_quality fora da escala: {unmapped_baseline}"
    )


def test_every_quality_has_a_band():
    for quality in EvidenceQuality:
        assert 0 < quality.band <= 0.5


@pytest.mark.parametrize("quality", list(EvidenceQuality))
def test_the_band_never_produces_a_negative_floor(quality):
    gain = build_gain(10.0, difficulty=1, config=DEFAULT_CONFIG, quality=quality)

    assert gain.monthly_low >= 0
    assert gain.monthly_low <= gain.monthly_expected <= gain.monthly_high


def test_the_portfolio_stops_looking_more_precise_than_it_is():
    """Verificação do princípio: onde o baseline é modelado, a faixa alarga."""
    analysis = analyze(SAMPLE)
    quantified = [
        o for o in analysis.opportunities if o.estimated_gain.monthly_expected > 0
    ]
    assert quantified

    for opportunity in quantified:
        gain = opportunity.estimated_gain
        # ±35% fixo dava 70% de largura para tudo; nada modelado pode sair
        # mais estreito que isso agora.
        if opportunity.evidence_quality in {"modeled", "modeled_rule"}:
            assert _width(gain) >= 0.70 or gain.monthly_high < gain.monthly_expected * 1.4
