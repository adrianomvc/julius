"""A fila que transforma achado fora do catálogo em candidato a regra."""

from __future__ import annotations

import json

import pytest

from julius.analysis import append_candidates
from julius.analysis.response_validator import (
    AgentOutputError,
    ContextualAnalysis,
    EvidenceRef,
    UncoveredFinding,
)


def _analysis(*findings: UncoveredFinding, scan_id: str = "scan-1") -> ContextualAnalysis:
    return ContextualAnalysis(
        account="123456789012",
        scan_id=scan_id,
        executive_summary="resumo",
        implementation_order=[],
        recommendations=[],
        uncovered_findings=list(findings),
    )


def _finding(
    proposed: str = "GLUE-CODE-BROADCAST-MISSING",
    sha256: str = "a" * 64,
) -> UncoveredFinding:
    return UncoveredFinding(
        title="Join sem broadcast em tabela pequena",
        asset_type="glue_job",
        asset_name="job-a",
        evidence_ref=EvidenceRef(sha256=sha256, lines=[42]),
        why_not_covered="nenhuma regra observa o tamanho do lado direito do join",
        proposed_rule_id=proposed,
        confidence_basis="tamanho da tabela lido do catálogo no próprio script",
    )


def test_same_pattern_across_scans_accumulates_instead_of_duplicating(tmp_path):
    """Um padrão visto uma vez é anedota; repetido, é candidato a regra."""
    path = tmp_path / "rule-candidates.json"

    assert append_candidates(_analysis(_finding(), scan_id="scan-1"), path) == 1
    assert append_candidates(_analysis(_finding(), scan_id="scan-2"), path) == 1

    rows = json.loads(path.read_text(encoding="utf-8"))["candidates"]
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2
    assert rows[0]["first_seen_scan"] == "scan-1"
    assert rows[0]["last_seen_scan"] == "scan-2"


def test_distinct_artifact_or_rule_is_a_separate_candidate(tmp_path):
    path = tmp_path / "rule-candidates.json"
    append_candidates(_analysis(_finding()), path)
    append_candidates(_analysis(_finding(sha256="b" * 64)), path)
    append_candidates(_analysis(_finding(proposed="ATHENA-CROSS-JOIN")), path)

    rows = json.loads(path.read_text(encoding="utf-8"))["candidates"]
    assert len(rows) == 3


def test_frequency_orders_the_queue_for_human_review(tmp_path):
    path = tmp_path / "rule-candidates.json"
    append_candidates(_analysis(_finding()), path)
    append_candidates(_analysis(_finding(proposed="ATHENA-CROSS-JOIN")), path)
    append_candidates(_analysis(_finding()), path)

    rows = json.loads(path.read_text(encoding="utf-8"))["candidates"]
    assert rows[0]["proposed_rule_id"] == "GLUE-CODE-BROADCAST-MISSING"
    assert rows[0]["occurrences"] == 2


def test_analysis_without_uncovered_findings_leaves_no_file(tmp_path):
    path = tmp_path / "rule-candidates.json"
    assert append_candidates(_analysis(), path) == 0
    assert not path.exists()


def test_corrupt_queue_fails_loudly_instead_of_being_overwritten(tmp_path):
    path = tmp_path / "rule-candidates.json"
    path.write_text("{ nao e json", encoding="utf-8")

    with pytest.raises(AgentOutputError, match="ilegível"):
        append_candidates(_analysis(_finding()), path)
