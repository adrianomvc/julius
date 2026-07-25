"""Sessão boto3 read-only via perfis AWS CLI SSO."""

from __future__ import annotations

import boto3


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Sessão via cadeia de credenciais SSO do AWS CLI."""
    return boto3.Session(profile_name=profile or None, region_name=region or None)
