"""Julius como ferramenta local usada pelo agente Devin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from julius.analysis import (
    AgentOutputError,
    build_agent_context,
    load_agent_context,
    prepare_agent_workspace,
    validate_agent_output,
    validate_result_file,
)
from julius.cli import app
from julius.collection.redaction import redact_secrets
from julius.pipeline import analyze
from julius.reporting import renderer
from julius.reporting.contextual import attach_contextual_analysis

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "consumer-avi.json"


@pytest.fixture()
def context():
    return build_agent_context(analyze(SAMPLE), top=5)


def _valid_result(context) -> dict:
    ids = [item["opportunity_id"] for item in context.opportunities]
    recommendations = []
    for item in context.opportunities:
        url = (
            item["doc_links"][0]
            if item["doc_links"]
            else "https://docs.aws.amazon.com/whitepapers/latest/cost-optimization-pillar/"
        )
        recommendations.append(
            {
                "opportunity_id": item["opportunity_id"],
                "contextual_diagnosis": "A evidência confirma que a ação deve ser testada.",
                "recommendation": "Aplicar primeiro em ambiente controlado.",
                "implementation_steps": ["Revisar a configuração atual."],
                "validation_steps": ["Comparar a métrica antes e depois."],
                "dependencies": [],
                "conflicts": [],
                "risks": ["Mudança deve respeitar a janela operacional."],
                "documentation": [
                    {
                        "title": "Documentação oficial AWS",
                        "url": url,
                        "relevance": "Descreve a configuração recomendada.",
                    }
                ],
                "assumptions": [],
                "missing_evidence": item["missing_evidence"],
            }
        )
    return {
        "account": context.account["id"],
        "scan_id": context.scan_id,
        "executive_summary": "Plano contextual baseado nas evidências do Julius.",
        "implementation_order": ids,
        "recommendations": recommendations,
        "signal_verdicts": [
            {
                "rule_id": signal["rule_id"],
                "asset_name": signal["asset_name"],
                "verdict": "needs_evidence",
                "rationale": "O artefato não permite confirmar o padrão sozinho.",
                "evidence_ref": {
                    "sha256": signal["artifact_sha256"],
                    "lines": signal["lines"],
                },
            }
            for signal in context.signals
        ],
        "uncovered_findings": [],
    }


def _expected_signals(context) -> dict[tuple[str, str], str]:
    return {
        (item["rule_id"], item["asset_name"]): item["artifact_sha256"]
        for item in context.signals
    }


def test_context_preserves_deterministic_values_and_read_only_boundary(context):
    source = analyze(SAMPLE).opportunities[0]
    prepared = context.opportunities[0]

    assert prepared["deterministic"]["estimated_gain"]["monthly_expected"] >= 0
    assert "execution_priority" in prepared["deterministic"]
    assert context.constraints["allow_mutations"] is False
    assert context.constraints["allow_resource_deletion"] is False
    assert context.constraints["allow_email_send"] is False
    assert source.opportunity_id


def test_context_redacts_common_secret_shapes():
    text = (
        "password=super-secret token:abc123 "
        "AKIAIOSFODNN7EXAMPLE Bearer header.payload.signature"
    )
    redacted = redact_secrets(text)

    assert "super-secret" not in redacted
    assert "abc123" not in redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "header.payload.signature" not in redacted


def test_structured_result_accepts_only_context_ids_and_official_aws_docs(context):
    valid = validate_agent_output(
        _valid_result(context),
        account=context.account["id"],
        scan_id=context.scan_id,
        allowed_opportunity_ids={
            item["opportunity_id"] for item in context.opportunities
        },
        expected_signals=_expected_signals(context),
    )
    assert len(valid.recommendations) == len(context.opportunities)
    assert len(valid.signal_verdicts) == len(context.signals)

    invalid = _valid_result(context)
    invalid["recommendations"][0]["documentation"][0]["url"] = (
        "https://example.com/invented"
    )
    with pytest.raises(AgentOutputError, match="domínio oficial"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
        )


def test_result_cannot_inject_or_replace_deterministic_fields(context):
    invalid = _valid_result(context)
    invalid["recommendations"][0]["estimated_gain"] = 999999

    with pytest.raises(AgentOutputError, match="não permitidos"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
        )


def test_prepare_and_validate_are_fully_local(tmp_path):
    analysis = analyze(SAMPLE)
    context, files = prepare_agent_workspace(analysis, tmp_path, top=3)
    assert {path.name for path in files} == {
        "context.json",
        "instructions.md",
        "output-schema.json",
    }
    assert "api.devin.ai" not in (tmp_path / "instructions.md").read_text(
        encoding="utf-8"
    )

    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(_valid_result(context), ensure_ascii=False),
        encoding="utf-8",
    )
    validated = validate_result_file(tmp_path / "context.json", result_path)
    assert validated.account == analysis.account.account_id


def test_agent_cli_prepares_workspace_without_external_service(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "prepare",
            "--input",
            str(SAMPLE),
            "--output",
            str(tmp_path),
            "--top",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Nenhum serviço externo foi chamado" in result.output
    assert (tmp_path / "context.json").exists()

    prepared = load_agent_context(tmp_path / "context.json")
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(_valid_result(prepared), ensure_ascii=False),
        encoding="utf-8",
    )
    validation = CliRunner().invoke(
        app,
        [
            "agent",
            "validate",
            "--context",
            str(tmp_path / "context.json"),
            "--result",
            str(result_path),
        ],
    )
    assert validation.exit_code == 0, validation.output
    assert "Análise Devin válida" in validation.output
    assert (tmp_path / "validated-result.json").exists()


def test_validated_ai_context_enriches_delivery_without_changing_priority(context):
    analysis = analyze(SAMPLE, scan_id=context.scan_id)
    before = [
        (item.opportunity_id, item.execution_priority, item.estimated_gain.monthly_expected)
        for item in analysis.opportunities
    ]
    contextual = validate_agent_output(
        _valid_result(context),
        account=context.account["id"],
        scan_id=context.scan_id,
        allowed_opportunity_ids={
            item["opportunity_id"] for item in context.opportunities
        },
        expected_signals=_expected_signals(context),
    )
    attach_contextual_analysis(analysis.vm, contextual)
    html = renderer.render_html(analysis.vm)
    email_html, email_text = renderer.render_email(analysis.vm)
    report_json = json.loads(renderer.render_json(analysis.vm, analysis.opportunities))
    after = [
        (item.opportunity_id, item.execution_priority, item.estimated_gain.monthly_expected)
        for item in analysis.opportunities
    ]

    assert before == after
    assert "Análise contextual pelo Devin" in html
    assert "ANÁLISE CONTEXTUAL PELO DEVIN" in email_html
    assert "ANÁLISE CONTEXTUAL PELO DEVIN" in email_text
    assert report_json["ai_analysis"]["source"] == "Devin"


def test_report_cli_accepts_only_context_bound_validated_result(tmp_path):
    analysis = analyze(SAMPLE)
    context, _ = prepare_agent_workspace(analysis, tmp_path / "agent", top=3)
    result_path = tmp_path / "agent" / "result.json"
    result_path.write_text(
        json.dumps(_valid_result(context), ensure_ascii=False),
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    result = CliRunner().invoke(
        app,
        [
            "report",
            "--input",
            str(SAMPLE),
            "--output",
            str(report_dir),
            "--store",
            str(tmp_path / "backlog.json"),
            "--history-db",
            str(tmp_path / "history.duckdb"),
            "--parquet-dir",
            str(tmp_path / "parquet"),
            "--agent-context",
            str(tmp_path / "agent" / "context.json"),
            "--agent-result",
            str(result_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Análise contextual pelo Devin" in (
        report_dir / "report.html"
    ).read_text(encoding="utf-8")


def test_context_declares_its_own_coverage_and_the_silence_it_carries(context):
    """O recorte precisa ser visível: o que ficou fora não é o que não existe."""
    full = analyze(SAMPLE)

    assert context.schema_version == "1.1"
    assert context.portfolio["analyzed"] == len(context.opportunities)
    assert context.portfolio["total_opportunities"] == len(full.opportunities)
    assert context.portfolio["analyzed"] <= context.portfolio["total_opportunities"]
    assert "rule_families_without_evidence" in context.constraints
    for family in context.constraints["rule_families_without_evidence"]:
        assert family["service"] and family["name"] and family["requires"]


def test_empty_recommendation_text_is_not_a_valid_analysis(context):
    """Estrutura correta com texto vazio passava como análise completa."""
    invalid = _valid_result(context)
    invalid["recommendations"][0]["recommendation"] = "   "

    with pytest.raises(AgentOutputError, match="não podem ser vazios"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
            expected_signals=_expected_signals(context),
        )


def test_recommendation_must_say_how_to_act_or_what_is_missing(context):
    invalid = _valid_result(context)
    invalid["recommendations"][0]["implementation_steps"] = []
    invalid["recommendations"][0]["missing_evidence"] = []

    with pytest.raises(AgentOutputError, match="implementation_steps ou missing_evidence"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
            expected_signals=_expected_signals(context),
        )


def test_every_signal_needs_a_verdict(context):
    """Silêncio sobre um sinal não é veredito."""
    if not context.signals:
        pytest.skip("dataset de exemplo não produziu sinais")
    invalid = _valid_result(context)
    invalid["signal_verdicts"] = invalid["signal_verdicts"][:-1]

    with pytest.raises(AgentOutputError, match="precisa de veredito"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
            expected_signals=_expected_signals(context),
        )


def test_uncovered_finding_cannot_reuse_an_existing_rule_id(context):
    """Se já existe regra para o padrão, não é lacuna de catálogo."""
    existing = context.opportunities[0]["rule_id"]
    invalid = _valid_result(context)
    invalid["uncovered_findings"] = [
        {
            "title": "padrão observado",
            "asset_type": "glue_job",
            "asset_name": "job-a",
            "evidence_ref": {"sha256": "", "lines": []},
            "why_not_covered": "motivo",
            "proposed_rule_id": existing,
            "confidence_basis": "base",
        }
    ]

    with pytest.raises(AgentOutputError, match="colide com regra existente"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
            expected_signals=_expected_signals(context),
            known_rule_ids={existing},
        )


def test_conclusion_about_a_script_must_name_the_script(context):
    """Hash desconhecido é suposição, não leitura do artefato."""
    invalid = _valid_result(context)
    invalid["uncovered_findings"] = [
        {
            "title": "padrão observado",
            "asset_type": "glue_job",
            "asset_name": "job-a",
            "evidence_ref": {"sha256": "f" * 64, "lines": [10]},
            "why_not_covered": "motivo",
            "proposed_rule_id": "GLUE-CODE-INVENTADA",
            "confidence_basis": "base",
        }
    ]

    with pytest.raises(AgentOutputError, match="fora do pacote"):
        validate_agent_output(
            invalid,
            account=context.account["id"],
            scan_id=context.scan_id,
            allowed_opportunity_ids={
                item["opportunity_id"] for item in context.opportunities
            },
            expected_signals=_expected_signals(context),
            known_artifact_hashes={"a" * 64},
        )
