"""Arestas tipadas e auditáveis do grafo."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from julius.graph.assets import AssetKey


class EdgeType(StrEnum):
    SCHEDULE_TRIGGERS_STATE_MACHINE = "SCHEDULE_TRIGGERS_STATE_MACHINE"
    STATE_MACHINE_RUNS_JOB = "STATE_MACHINE_RUNS_JOB"
    JOB_READS_TABLE = "JOB_READS_TABLE"
    JOB_WRITES_TABLE = "JOB_WRITES_TABLE"
    QUERY_READS_TABLE = "QUERY_READS_TABLE"
    TABLE_USED_BY_ACCOUNT = "TABLE_USED_BY_ACCOUNT"
    TABLE_PUBLISHED_BY_DATAWARM = "TABLE_PUBLISHED_BY_DATAWARM"
    ASSET_OWNED_BY_SQUAD = "ASSET_OWNED_BY_SQUAD"


@dataclass(frozen=True)
class Edge:
    source: AssetKey
    target: AssetKey
    type: EdgeType
    evidence: str
    confidence: float = 1.0
