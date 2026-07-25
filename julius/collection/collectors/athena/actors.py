"""Atribuição de ator por CloudTrail e Identity Center.

Nunca persiste `userIdentity`, IP ou o bloco bruto do evento."""

from __future__ import annotations

import json
import re
from typing import Any

from julius.collection.collectors.athena.evidence import arn_tail

_AUTOMATION = re.compile(r"(service|automation|pipeline|scheduler|airflow|lambda|glue|states)", re.I)


def resolve_actor(event: dict[str, Any] | None) -> tuple[str, str, str, str, str | None]:
    """Resolve ator sem expor o evento ou identificadores sensíveis."""
    if not event:
        return "desconhecido", "unknown", "unknown", "low", None
    identity = event.get("userIdentity") or {}
    session = identity.get("sessionContext") or {}
    attrs = session.get("attributes") or {}
    on_behalf = identity.get("onBehalfOf") or session.get("onBehalfOf") or {}
    if on_behalf.get("userId"):
        return str(on_behalf["userId"]), "human", "identity_center", "high", None
    source = identity.get("sourceIdentity") or session.get("sourceIdentity") or attrs.get("sourceIdentity")
    if source:
        return str(source), "human", "source_identity", "high", None
    if identity.get("type") == "IAMUser":
        return str(identity.get("userName") or arn_tail(identity.get("arn"))), "human", "iam_user", "high", None
    if identity.get("type") == "AssumedRole":
        raw_identity = identity.get("arn") or identity.get("principalId")
        name = arn_tail(raw_identity)
        actor_type = "automation" if _AUTOMATION.search(str(raw_identity or "")) else "role_session"
        return name, actor_type, "assumed_role", "medium", None
    principal = identity.get("invokedBy") or identity.get("type")
    if principal:
        return str(principal), "automation", "service", "medium", None
    return "desconhecido", "unknown", "unknown", "low", None


def enrich_actors(items, cloudtrail, identitystore, start, end, telemetry):
    if cloudtrail is None:
        return
    telemetry.used("Athena CloudTrail")
    if identitystore is not None:
        telemetry.used("Athena Identity Center")
    by_id = {item.query_execution_id: item for item in items}
    try:
        paginator = cloudtrail.get_paginator("lookup_events")
        pages = paginator.paginate(
            LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "StartQueryExecution"}],
            StartTime=start,
            EndTime=end,
        )
        for page in pages:
            for wrapper in page.get("Events", []):
                try:
                    event = json.loads(wrapper.get("CloudTrailEvent") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                query_id = ((event.get("responseElements") or {}).get("queryExecutionId"))
                item = by_id.get(query_id)
                if item is None:
                    continue
                actor, kind, source, confidence, email = resolve_actor(event)
                if source == "identity_center" and identitystore is not None:
                    actor, email, confidence = describe_identity(
                        identitystore, event, actor, telemetry
                    )
                item.actor, item.actor_type = actor, kind
                item.identity_source, item.identity_confidence = source, confidence
                item.actor_email = email
    except Exception as exc:
        telemetry.failed("Athena CloudTrail", exc)


def describe_identity(client, event, user_id, telemetry):
    identity = event.get("userIdentity") or {}
    session = identity.get("sessionContext") or {}
    on_behalf = identity.get("onBehalfOf") or session.get("onBehalfOf") or {}
    store = on_behalf.get("identityStoreId")
    arn = on_behalf.get("identityStoreArn") or ""
    if not store and ":identitystore/" in arn:
        store = arn.rsplit("/", 1)[-1]
    if not store:
        return user_id, None, "medium"
    try:
        user = client.describe_user(IdentityStoreId=store, UserId=user_id)
        name = user.get("DisplayName") or user.get("UserName") or user_id
        emails = user.get("Emails") or []
        email = next((entry.get("Value") for entry in emails if entry.get("Primary")), None)
        return str(name), email, "high"
    except Exception as exc:
        telemetry.failed("Athena Identity Center", exc)
        return user_id, None, "medium"
