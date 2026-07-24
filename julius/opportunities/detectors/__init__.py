"""Detectores determinísticos ativos no MVP 1B."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import Account
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors import (
    athena,
    crawlers,
    data,
    databrew,
    glue,
    sagemaker,
    sessions,
    stepfunctions,
)


def run_all(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    found: list[Opportunity] = []
    found += glue.detect(account, config, scan_id)
    found += sessions.detect(account, config, scan_id)
    found += crawlers.detect(account, config, scan_id)
    found += databrew.detect(account, config, scan_id)
    found += athena.detect(account, config, scan_id)
    found += stepfunctions.detect(account, config, scan_id)
    found += sagemaker.detect(account, config, scan_id)
    found += data.detect(account, config, scan_id)
    return found
