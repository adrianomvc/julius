"""Política explícita do que o Julius pode coletar e recomendar por perfil."""

from __future__ import annotations

from dataclasses import dataclass

CONSUMER_DATAMESH = "consumer_datamesh"
FULL_ANALYSIS = "full_analysis"


@dataclass(frozen=True)
class ScopePolicy:
    """Capacidades habilitadas e modo de tratamento do S3."""

    profile: str
    enabled_capabilities: frozenset[str]
    s3_mode: str

    def allows(self, *capabilities: str) -> bool:
        return all(item in self.enabled_capabilities for item in capabilities)


_COMMON = frozenset(
    {
        "billing",
        "glue_jobs",
        "glue_interactive_sessions",
        "glue_catalog",
        "athena",
        "stepfunctions",
        "sagemaker",
        "s3_evidence",
        "redshift",
        "ownership",
    }
)

_POLICIES = {
    CONSUMER_DATAMESH: ScopePolicy(
        profile=CONSUMER_DATAMESH,
        enabled_capabilities=_COMMON,
        s3_mode="storage_class_only",
    ),
    FULL_ANALYSIS: ScopePolicy(
        profile=FULL_ANALYSIS,
        enabled_capabilities=_COMMON
        | frozenset({"glue_crawlers", "glue_databrew"}),
        s3_mode="proposal",
    ),
}


def policy_for_profile(profile: str | None) -> ScopePolicy:
    """Resolve um perfil conhecido; datasets antigos preservam análise completa."""
    name = (profile or FULL_ANALYSIS).strip()
    try:
        return _POLICIES[name]
    except KeyError as exc:
        raise ValueError(f"scope_profile inválido: {name}") from exc


def available_scope_profiles() -> tuple[str, ...]:
    return tuple(_POLICIES)
