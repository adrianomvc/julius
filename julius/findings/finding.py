"""O que uma regra viu — sem ação, sem dinheiro, sem pontuação."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """A observação em si: qual regra, sobre qual ativo, e por quê.

    Separado de `Recommendation` porque as duas mudam por motivos diferentes:
    o que se observa depende do que a AWS expõe; o que se recomenda depende de
    como a AWS cobra e do apetite de risco de quem opera.
    """

    rule_id: str
    rule_version: str
    asset_type: str
    asset_name: str
    title: str
    why: str
    # Processo gerador (linhagem) que pode ser pausado — quando aplicável.
    source_process: str | None = None
