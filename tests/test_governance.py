"""Testes da governança determinística (candidatos a Producer calculados)."""

from __future__ import annotations

from pathlib import Path

from julius.collection.normalizers import load_account
from julius.governance import compute_candidates, recommend
from julius.pipeline import analyze

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "sample"


def test_producer_candidates_computed_from_signals():
    account = load_account(SAMPLE_DIR / "consumer-nova.json")
    account.producer_candidates = []  # força o cálculo
    cands = compute_candidates(account)
    by_name = {c.name: c for c in cands}
    c = by_name["recomendacoes_regionais"]
    # 3 comunidades + toques altos + DataWarm + owner/linhagem → Migrar.
    assert c.candidate_score >= 60
    assert c.readiness_score >= 55
    assert recommend(c.candidate_score, c.readiness_score).label == "Migrar"


def test_unused_table_is_not_a_producer_candidate():
    # Base sem toques não vira candidato a Producer (é caso dos detectores de dados).
    account = load_account(SAMPLE_DIR / "consumer-avi.json")
    account.producer_candidates = []
    names = {c.name for c in compute_candidates(account)}
    assert "base_legado_diaria" not in names


def test_ingested_candidates_take_precedence():
    # consumer-avi traz candidatos ingeridos → não são sobrescritos pelo cálculo.
    a = analyze(SAMPLE_DIR / "consumer-avi.json")
    names = {p.name for p in a.vm.producers}
    assert "produto_recomendacoes" in names  # veio do dataset, preservado
