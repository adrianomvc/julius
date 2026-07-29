"""Continuidade local das evidências financeiras do SageMaker."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from julius.collection.models import Account
from julius.collection.normalizers.loader import load_account


def carry_consistent_scans(account: Account, previous_path: str | Path) -> None:
    """Incrementa a sequência quando a condição material persiste.

    A coleta continua sem banco externo: o próprio ``--output`` anterior é o
    checkpoint. Mudanças na condição reiniciam a sequência, e arquivo ausente
    ou inválido simplesmente mantém o primeiro scan.
    """
    path = Path(previous_path)
    if not path.is_file():
        return
    try:
        previous = load_account(path)
    except (OSError, TypeError, ValueError):
        return
    if previous.account_id != account.account_id:
        return

    _carry(
        account.sagemaker_apps,
        previous.sagemaker_apps,
        lambda item: (
            item.status,
            item.instance_type,
            item.idle_shutdown_min,
            round(item.idle_hours_per_day, 1),
            item.activity_metrics_available,
        ),
    )
    _carry(
        account.sagemaker_endpoints,
        previous.sagemaker_endpoints,
        lambda item: (
            item.status,
            item.mode,
            item.instance_type,
            item.instance_count,
            item.min_capacity,
            item.provisioned_concurrency,
            item.invocations == 0,
        ),
    )
    _carry(
        account.sagemaker_spaces,
        previous.sagemaker_spaces,
        lambda item: (
            item.status,
            item.ebs_volume_size_gb,
            item.active_app_count,
        ),
    )
    _carry(
        account.sagemaker_domains,
        previous.sagemaker_domains,
        lambda item: (
            item.status,
            item.home_efs_file_system_id,
            item.active_app_count,
            item.efs_total_io_bytes == 0,
        ),
    )
    _carry(
        account.sagemaker_feature_groups,
        previous.sagemaker_feature_groups,
        lambda item: (
            item.status,
            item.throughput_mode,
            item.provisioned_read_capacity,
            item.provisioned_write_capacity,
            item.throttled_requests == 0,
        ),
    )
    _carry(
        account.sagemaker_jobs,
        previous.sagemaker_jobs,
        lambda item: (
            item.kind,
            item.status,
            item.instance_type,
            item.failure_category,
        ),
    )


def _carry(current: list[Any], previous: list[Any], signature: Callable[[Any], tuple]) -> None:
    before = {item.name: item for item in previous}
    for item in current:
        old = before.get(item.name)
        item.consistent_scans = (
            int(old.consistent_scans or 1) + 1
            if old is not None and signature(old) == signature(item)
            else 1
        )
