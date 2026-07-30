from __future__ import annotations

from dataclasses import replace
from datetime import date

from julius.collection.models import Account, RedshiftCluster
from julius.config import DEFAULT_CONFIG
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation
from julius.findings.recommendation import Recommendation
from julius.knowledge.rules import RuleFamily
from julius.knowledge.rules import families_without_evidence as missing_families
from julius.knowledge.rules.redshift import rules as redshift_rules


def _family(*, capability: str = "glue_jobs", requires=(), measures=()) -> RuleFamily:
    return RuleFamily(
        service="test",
        name=f"{capability}-{requires}-{measures}",
        detect=lambda _account, _config, _scan: [],
        required_capabilities=frozenset({capability}),
        requires=requires,
        measures=measures,
    )


def test_disabled_family_never_appears_as_missing_evidence(monkeypatch):
    family = _family(capability="glue_crawlers", measures=("glue_jobs.avg_cpu_load",))
    monkeypatch.setattr("julius.knowledge.rules.REGISTRY", [family])
    account = Account(account_id="123", scope_profile="consumer_datamesh")
    assert missing_families(account) == []


def test_enabled_family_with_missing_measure_is_reported(monkeypatch):
    family = _family(measures=("glue_jobs.avg_cpu_load",))
    monkeypatch.setattr("julius.knowledge.rules.REGISTRY", [family])
    account = Account(account_id="123", scope_profile="consumer_datamesh")
    account.glue_jobs = [type("Job", (), {"avg_cpu_load": None})()]
    assert missing_families(account) == [family]


def test_enabled_family_with_empty_inventory_is_reported(monkeypatch):
    family = _family(requires=("glue_jobs",))
    monkeypatch.setattr("julius.knowledge.rules.REGISTRY", [family])
    account = Account(account_id="123", scope_profile="consumer_datamesh")
    assert missing_families(account) == [family]


def test_stale_pricing_blocks_modeled_value_from_portfolio():
    pricing = replace(
        DEFAULT_CONFIG.pricing,
        verified=True,
        verified_at="2026-01-01",
        verification={"glue": {"verified": True, "verified_at": "2026-01-01"}},
    )
    config = replace(DEFAULT_CONFIG, pricing=pricing)
    opportunity = build(
        Finding(
            rule_id="TEST-PRICE",
            rule_version="1",
            asset_type="glue_job",
            asset_name="job",
            title="teste",
            why="preço modelado",
        ),
        Recommendation(
            difficulty=1,
            action="testar",
            how_to_apply="não aplicar automaticamente",
            how_to_validate="comparar resultado",
        ),
        Evidence(items=["modelo"], has_optional_metrics=True),
        Estimation(
            method="test",
            baseline_cost=100,
            projected_cost=50,
            estimated_saving=50,
            saving_quality="modeled_evidence",
            pricing_dependencies=("glue",),
        ),
        RuleContext(
            account="123",
            config=config,
            scan_id="scan",
            today=date(2026, 7, 30),
        ),
    )
    assert opportunity.estimation.saving_quality == "unavailable"
    assert opportunity.include_in_portfolio is False


def test_partial_pricing_verification_does_not_reverify_other_sections():
    pricing = replace(
        DEFAULT_CONFIG.pricing,
        verified=True,
        verified_at="2026-07-30",
        verification={"s3": {"verified": True, "verified_at": "2026-07-30"}},
    )
    assert pricing.dependencies_are_current(
        ("s3",), today=date(2026, 7, 30)
    )
    assert not pricing.dependencies_are_current(
        ("glue",), today=date(2026, 7, 30)
    )


def test_redshift_consumer_guardrails_are_signals_without_savings():
    account = Account(
        account_id="123",
        scope_profile="consumer_datamesh",
        redshift_clusters=[
            RedshiftCluster(
                name="wg",
                kind="serverless",
                max_rpu=None,
                serverless_usage_limits=[],
                advisor_recommendations=[
                    {"type": "DISTKEY", "text": "Revisar distribuição"}
                ],
            )
        ],
    )
    signals = {item.rule_id for item in redshift_rules.signals(account, DEFAULT_CONFIG)}
    assert {
        "REDSHIFT-ADVISOR-UNAPPLIED",
        "REDSHIFT-SERVERLESS-MAX-CAPACITY-MISSING",
        "REDSHIFT-SERVERLESS-RPU-HOURS-LIMIT-MISSING",
    } <= signals
