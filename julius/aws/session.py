"""Sessão boto3 read-only (perfil/região + assume-role opcional para multi-conta)."""

from __future__ import annotations

import boto3


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Sessão via cadeia de credenciais do AWS CLI (SSO/perfil/role/execução)."""
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def assume_role(
    base: boto3.Session,
    role_arn: str,
    region: str | None = None,
    session_name: str = "julius-readonly",
) -> boto3.Session:
    """Assume uma role read-only em outra conta (STS AssumeRole) — multi-conta."""
    sts = base.client("sts")
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region or base.region_name,
    )
