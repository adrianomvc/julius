"""Atribuição de pessoa/ator por tag e eventos CloudTrail."""

from __future__ import annotations

from dataclasses import dataclass

from julius.collection.models import Account
from julius.graph.ownership import asset_owner_tag


@dataclass(frozen=True)
class ActorAttribution:
    actor: str | None
    source: str | None
    confidence: float


def resolve_actor(account: Account, asset_type: str, asset_name: str) -> ActorAttribution:
    owner_tag = asset_owner_tag(account, asset_type, asset_name)
    if owner_tag:
        return ActorAttribution(owner_tag, "tag Owner do recurso", 1.0)

    events = [
        event
        for event in account.actor_events
        if event.resource_type == asset_type and event.resource_name == asset_name
    ]
    events.sort(key=lambda event: event.event_time, reverse=True)

    for event in events:
        if event.source_identity:
            return ActorAttribution(event.source_identity, "CloudTrail sourceIdentity", 0.95)
    for event in events:
        session_name = _session_name(event.user_arn)
        if session_name:
            return ActorAttribution(session_name, "CloudTrail sessão SSO", 0.8)
    return ActorAttribution(None, None, 0.0)


def _session_name(user_arn: str | None) -> str | None:
    if not user_arn or ":assumed-role/" not in user_arn:
        return None
    parts = user_arn.split("/")
    if len(parts) < 3:
        return None
    candidate = parts[-1].strip()
    if not candidate or candidate.lower().startswith(("aws-", "botocore-")):
        return None
    return candidate
