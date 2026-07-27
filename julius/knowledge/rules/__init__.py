"""O registro de regras.

`run_all` era uma sequência fixa de oito chamadas. Regra nova significava editar
essa função, e os metadados de cada família — serviço, o que ela exige do
inventário, qual documentação sustenta — ficavam espalhados dentro de cada
detector, sem ninguém conseguir listá-los.

Aqui uma família de regras é uma entrada de `REGISTRY`: adicionar Redshift é
acrescentar um módulo e uma linha, sem tocar em nada existente. E o registro é
inspecionável — o relatório pode dizer quais regras rodaram, e a calibração pode
versionar por família.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from julius.collection.models import Account
from julius.findings.opportunity import Opportunity
from julius.findings.signal import Signal
from julius.knowledge.rules.athena import queries as athena_queries
from julius.knowledge.rules.cross_service import pipelines as cross_service_rules
from julius.knowledge.rules.data import rules as data_rules
from julius.knowledge.rules.glue import crawlers as glue_crawlers
from julius.knowledge.rules.glue import databrew as glue_databrew
from julius.knowledge.rules.glue import jobs as glue_jobs
from julius.knowledge.rules.glue import sessions as glue_sessions
from julius.knowledge.rules.redshift import rules as redshift_rules
from julius.knowledge.rules.sagemaker import rules as sagemaker_rules
from julius.knowledge.rules.stepfunctions import rules as stepfunctions_rules


@dataclass(frozen=True)
class RuleFamily:
    """Um conjunto de regras sobre um tipo de ativo."""

    service: str
    name: str
    detect: Callable[[Account, Any, str], list[Opportunity]]
    # O que a família precisa encontrar no inventário para ter o que dizer.
    # É documentação executável: se a coleta da fonte falhou, dá para explicar
    # por que a família não produziu nada em vez de o silêncio parecer "tudo ok".
    requires: tuple[str, ...] = ()
    # A medição sem a qual a família não pode concluir, como `"tables.touches_90d"`.
    # Diferente de `requires`: ali o inventário chega vazio; aqui ele chega cheio
    # e sem o número que importa. Os dois produzem o mesmo silêncio, e nenhum
    # dos dois pode passar por "está tudo bem".
    measures: tuple[str, ...] = ()
    docs: Sequence[str] = field(default_factory=tuple)
    # O que a família observa mas não consegue concluir. Fica fora de `detect`
    # de propósito: sinal não é achado, não recebe economia e não entra no
    # ranking — vai para a análise contextual julgar.
    signals: Callable[[Account, Any], list[Signal]] | None = None


REGISTRY: tuple[RuleFamily, ...] = (
    RuleFamily(
        service="glue",
        name="jobs",
        detect=glue_jobs.detect,
        requires=("glue_jobs",),
        signals=glue_jobs.signals,
    ),
    RuleFamily(
        service="glue",
        name="sessions",
        detect=glue_sessions.detect,
        requires=("interactive_sessions",),
        signals=glue_sessions.signals,
    ),
    RuleFamily(
        service="glue",
        name="crawlers",
        detect=glue_crawlers.detect,
        requires=("glue_crawlers",),
    ),
    RuleFamily(
        service="glue",
        name="databrew",
        detect=glue_databrew.detect,
        requires=("databrew_jobs",),
    ),
    RuleFamily(
        service="athena",
        name="queries",
        detect=athena_queries.detect,
        requires=("athena_queries",),
    ),
    RuleFamily(
        service="stepfunctions",
        name="state_machines",
        detect=stepfunctions_rules.detect,
        requires=("state_machines",),
        signals=stepfunctions_rules.signals,
    ),
    RuleFamily(
        service="sagemaker",
        name="apps_and_endpoints",
        detect=sagemaker_rules.detect,
        requires=("sagemaker_apps", "sagemaker_endpoints"),
        signals=sagemaker_rules.signals,
    ),
    RuleFamily(
        service="redshift",
        name="clusters",
        detect=redshift_rules.detect,
        requires=("redshift_clusters",),
        signals=redshift_rules.signals,
    ),
    RuleFamily(
        service="cross_service",
        name="wasted_production",
        detect=cross_service_rules.detect,
        # Só existe olhando os dois lados: quem escreve e quem lê.
        requires=("glue_jobs", "athena_queries"),
    ),
    RuleFamily(
        service="cross_service",
        name="data_products",
        detect=data_rules.detect,
        requires=("tables",),
        measures=("tables.touches_90d",),
    ),
)


def run_all(account: Account, config: Any, scan_id: str) -> list[Opportunity]:
    """Roda todas as famílias registradas, na ordem do registro."""
    found: list[Opportunity] = []
    for family in REGISTRY:
        found += family.detect(account, config, scan_id)
    return found


def collect_signals(account: Account, config: Any) -> list[Signal]:
    """Reúne o que as famílias observaram sem conseguir concluir."""
    found: list[Signal] = []
    for family in REGISTRY:
        if family.signals is not None:
            found += family.signals(account, config)
    return found


def families_without_evidence(account: Account) -> list[RuleFamily]:
    """Famílias que não tiveram o que analisar — silêncio explicado.

    Duas formas de não ter: o inventário chegou vazio, ou chegou cheio e sem a
    medição de que a família depende. A segunda é a mais traiçoeira, porque a
    seção do relatório fica vazia parecendo boa notícia.
    """
    return [
        family
        for family in REGISTRY
        if (
            family.requires
            and not any(getattr(account, name, None) for name in family.requires)
        )
        or any(_unmeasured(account, path) for path in family.measures)
    ]


def missing_evidence(account: Account, family: RuleFamily) -> str:
    """Por que esta família ficou sem analisar, em uma frase.

    Sem isso o relatório diria "tables" para uma conta cheia de tabelas, e o
    leitor concluiria que o Julius se enganou em vez de que faltou medida.
    """
    faltando = [path for path in family.measures if _unmeasured(account, path)]
    if faltando:
        return "medição ausente: " + ", ".join(faltando)
    return "inventário vazio: " + ", ".join(family.requires)


def _unmeasured(account: Account, path: str) -> bool:
    """Nenhum item da coleção tem a medição preenchida."""
    collection_name, _, field_name = path.partition(".")
    items = getattr(account, collection_name, None) or []
    if not items:
        # Coleção vazia já é coberta por `requires`; aqui não há o que dizer.
        return False
    return all(getattr(item, field_name, None) is None for item in items)


__all__ = [
    "REGISTRY",
    "RuleFamily",
    "collect_signals",
    "families_without_evidence",
    "missing_evidence",
    "run_all",
]
