"""Uma escala comparável sobre os três vocabulários de qualidade."""

from __future__ import annotations

import pytest

from julius.scoring.evidence_quality import (
    EvidenceQuality,
    combined,
    from_baseline,
    from_cost,
    from_saving,
)


def test_the_scale_is_ordered_so_two_findings_can_be_compared():
    """Era o que faltava: 'qual dos dois é mais confiável' tinha resposta."""
    assert EvidenceQuality.REALIZED > EvidenceQuality.MEASURED
    assert EvidenceQuality.MEASURED > EvidenceQuality.ALLOCATED
    assert EvidenceQuality.ALLOCATED > EvidenceQuality.ALLOCATED_PARTIAL
    assert EvidenceQuality.ALLOCATED_PARTIAL > EvidenceQuality.MODELED
    assert EvidenceQuality.MODELED > EvidenceQuality.MODELED_RULE


@pytest.mark.parametrize(
    "value,expected",
    [
        ("allocated", EvidenceQuality.ALLOCATED),
        ("allocated_partial", EvidenceQuality.ALLOCATED_PARTIAL),
        ("modeled", EvidenceQuality.MODELED),
    ],
)
def test_baseline_vocabulary_projects_onto_the_scale(value, expected):
    assert from_baseline(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("measured", EvidenceQuality.MEASURED),
        ("modeled_evidence", EvidenceQuality.MODELED),
        ("modeled_rule", EvidenceQuality.MODELED_RULE),
        ("unavailable", EvidenceQuality.MODELED_RULE),
    ],
)
def test_saving_vocabulary_projects_onto_the_scale(value, expected):
    assert from_saving(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("reconciled", EvidenceQuality.ALLOCATED),
        ("partial", EvidenceQuality.ALLOCATED_PARTIAL),
        ("unavailable", EvidenceQuality.MODELED),
    ],
)
def test_cost_vocabulary_projects_onto_the_scale(value, expected):
    assert from_cost(value) is expected


def test_an_unknown_value_degrades_instead_of_flattering():
    """Vocabulário novo que ninguém mapeou não pode virar 'confiável'."""
    assert from_baseline("inventado") is EvidenceQuality.MODELED_RULE
    assert from_saving("inventado") is EvidenceQuality.MODELED_RULE
    # Custo é a exceção deliberada: `unavailable` ali significa que a cobrança
    # não veio, e o baseline cai para tarifa — não para faixa de regra.
    assert from_cost("inventado") is EvidenceQuality.MODELED


def test_the_weakest_link_decides():
    """Baseline vindo da fatura não conserta uma economia estimada por regra."""
    assert combined("allocated", "modeled_rule") is EvidenceQuality.MODELED_RULE
    assert combined("modeled", "measured") is EvidenceQuality.MODELED
    assert combined("allocated", "measured") is EvidenceQuality.ALLOCATED
    assert combined("allocated_partial", "measured") is (
        EvidenceQuality.ALLOCATED_PARTIAL
    )


def test_being_anchored_in_billing_is_explicit():
    assert combined("allocated", "measured").is_anchored_in_billing
    assert combined("allocated_partial", "measured").is_anchored_in_billing
    assert not combined("modeled", "measured").is_anchored_in_billing
    assert not combined("allocated", "modeled_rule").is_anchored_in_billing


def test_every_level_has_a_label_for_the_report():
    assert all(level.label for level in EvidenceQuality)
    assert EvidenceQuality.MODELED_RULE.label == "Modelado por regra"


def test_the_report_carries_the_comparable_scale():
    """O relatório deixa de exigir três campos para ordenar por confiança."""
    import json
    from datetime import date

    from julius.pipeline import analyze
    from julius.reporting import renderer

    analysis = analyze(
        "data/sample/consumer-avi.json", today=date(2026, 7, 25), scan_id="q"
    )
    payload = json.loads(
        renderer.render_json(analysis.vm, analysis.opportunities)
    )
    rows = [
        item
        for item in payload["opportunities"]
        if item.get("evidence_quality")
    ]
    assert rows, "nenhuma oportunidade carregou a escala"
    valid = {level.name.lower() for level in EvidenceQuality}
    assert {item["evidence_quality"] for item in rows} <= valid
