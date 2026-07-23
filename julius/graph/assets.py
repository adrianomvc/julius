"""Nós normalizados do grafo de processos."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, order=True)
class AssetKey:
    account: str
    kind: str
    name: str

    @property
    def id(self) -> str:
        return f"{self.account}|{self.kind}|{self.name}"


@dataclass
class Asset:
    key: AssetKey
    attributes: dict[str, object] = field(default_factory=dict)
