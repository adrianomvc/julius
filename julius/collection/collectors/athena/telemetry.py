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
    #: Fonte que respondeu, mas só até onde o teto de paginação deixou. Não é
    #: falha — é evidência medida e incompleta, e as duas não podem virar a
    #: mesma linha na saúde.
    partials: dict[str, str] = field(default_factory=dict)
    _touched: set[str] = field(default_factory=set)

    def used(self, source: str) -> None:
        """Marca que a fonte foi consultada, mesmo que sem incidente."""
        self._touched.add(source)

    def partial(self, source: str, *, category: str, detail: str) -> None:
        """Evidência parcial: a fonte respondeu, a leitura foi interrompida.

        Só a primeira ocorrência vira `gap`. O truncamento acontece por tabela,
        e uma conta com quinhentas tabelas grandes produziria quinhentas linhas
        dizendo a mesma coisa — o relatório precisa do fato, não do inventário.
        """
        self.used(source)
        if source in self.partials:
            return
        self.partials[source] = category
        self.coverage.gaps.append(f"{source}: {detail}")

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
            # Falha manda sobre parcial: a fonte que quebrou não é a fonte que
            # respondeu até onde o teto deixou.
            status = "unavailable" if category else "ok"
            if not category and source in self.partials:
                status = "partial"
                category = self.partials[source]
            out.append(
                CollectionHealth(
                    source=source,
                    status=status,
                    started_at=moment,
                    completed_at=moment,
                    error_category=category,
                    impact=impact if category else "",
                    next_action=next_action if category else "",
                )
            )
        return out
