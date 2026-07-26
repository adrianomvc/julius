"""Saúde por fonte dentro da coleta Athena.

O Athena aparecia na saúde da coleta como uma linha só — `"Athena Queries"` —
enquanto o Glue tinha onze fontes rastreadas. Uma falha de CloudTrail, uma
permissão faltando no Glue Catalog e um Cost Explorer indisponível caíam todos
na mesma entrada, e a causa virava texto livre numa lista de `gaps` que ninguém
conseguia agregar nem alertar.

Aqui cada dependência da coleta Athena é uma fonte com nome, categoria estável
de erro, impacto e próxima ação — o mesmo contrato que o resto da coleta usa. O
texto legível continua indo para `coverage.gaps`, que o relatório exibe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from julius.collection.health.recorder import error_category
from julius.collection.models import AthenaCoverage, CollectionHealth

# Nome da fonte → (impacto, próxima ação). O prefixo mantém as entradas juntas
# na saúde e deixa claro que descrevem a coleta Athena, não o serviço inteiro.
SOURCES: dict[str, tuple[str, str]] = {
    "Athena API": (
        "sem histórico de execuções não há padrão de query a analisar",
        "validar athena:ListWorkGroups, ListQueryExecutions e BatchGetQueryExecution",
    ),
    "Athena CloudWatch": (
        "sem ProcessedBytes o volume medido não pode ser reconciliado",
        "habilitar métricas no workgroup e validar cloudwatch:GetMetricStatistics",
    ),
    "Athena CloudTrail": (
        "execuções ficam sem ator atribuído",
        "validar cloudtrail:LookupEvents",
    ),
    "Athena Identity Center": (
        "atores permanecem como identificador técnico, sem pessoa",
        "validar identitystore:DescribeUser",
    ),
    "Athena Glue Catalog": (
        "particionamento, formato e compressão das tabelas ficam incompletos",
        "validar glue:GetTable nas bases consultadas",
    ),
    "Athena S3": (
        "arquivos pequenos e compressão não podem ser confirmados",
        "validar s3:ListBucket nos prefixos das tabelas",
    ),
    "Athena Cost Explorer": (
        "custo por padrão de query permanece sem cobrança para ratear",
        "validar ce:GetCostAndUsage filtrado por Amazon Athena",
    ),
}

# Fontes cuja falha impede considerar a coleta reconciliada. Antes isso era uma
# busca por trechos de texto dentro dos gaps.
BLOCKING = ("Athena API", "Athena CloudWatch")


@dataclass
class AthenaTelemetry:
    """Acumula o que aconteceu com cada dependência da coleta Athena."""

    coverage: AthenaCoverage
    failures: dict[str, str] = field(default_factory=dict)
    _touched: set[str] = field(default_factory=set)

    def used(self, source: str) -> None:
        """Marca que a fonte foi consultada, mesmo que sem incidente."""
        self._touched.add(source)

    def failed(self, source: str, exc: Exception, *, detail: str = "") -> None:
        self.used(source)
        self.failures.setdefault(source, error_category(exc))
        self.coverage.gaps.append(
            f"{source}: {detail or type(exc).__name__}"
        )

    def unavailable(self, source: str, *, category: str, detail: str) -> None:
        self.used(source)
        self.failures.setdefault(source, category)
        self.coverage.gaps.append(f"{source}: {detail}")

    def blocked(self) -> bool:
        """Alguma fonte que impede reconciliação falhou?"""
        return any(source in self.failures for source in BLOCKING)

    def entries(self, *, started: datetime | None = None) -> list[CollectionHealth]:
        """Uma entrada de saúde por dependência consultada."""
        moment = (started or datetime.now(timezone.utc)).isoformat()
        out: list[CollectionHealth] = []
        for source in SOURCES:
            if source not in self._touched:
                continue
            impact, next_action = SOURCES[source]
            category = self.failures.get(source, "")
            out.append(
                CollectionHealth(
                    source=source,
                    status="unavailable" if category else "ok",
                    started_at=moment,
                    completed_at=moment,
                    error_category=category,
                    impact=impact if category else "",
                    next_action=next_action if category else "",
                )
            )
        return out
