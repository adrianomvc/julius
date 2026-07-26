"""Padrões que atravessam a fronteira entre serviços.

Cada família de regra até aqui enxerga um serviço só. Por isso ninguém via o
caso mais caro que existe num data lake: uma tabela que custa DPU-hora para ser
produzida no Glue e é lida no Athena de um jeito que joga fora o esforço — sem
filtro de partição, ou com scan completo.

Nenhum dos dois lados, sozinho, tem informação para acusar isso. O detector de
Glue vê um job caro que funciona. O detector de Athena vê uma query ruim, mas
não sabe quanto custou produzir o que ela varre.
"""

from __future__ import annotations

from julius.collection.models import Account, GlueJob
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation

_DOC_PARTITION = "https://docs.aws.amazon.com/athena/latest/ug/partitions.html"
_DOC_PUSHDOWN = (
    "https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-pushdown.html"
)


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    writers = _writers_by_table(account)
    out: list[Opportunity] = []
    seen: set[str] = set()

    for query in account.athena_queries:
        if query.modality not in {"on_demand", ""}:
            continue
        wasteful = query.full_scan_confirmed or bool(query.missing_partition_filters)
        if not wasteful:
            continue
        for table in query.reads_tables:
            writer = writers.get(table)
            if writer is None or table in seen:
                continue
            seen.add(table)
            out.append(_wasted_production(account, table, writer, query, config, scan_id))
    return out


def _writers_by_table(account: Account) -> dict[str, GlueJob]:
    """Tabela → job que a escreve, quando a linhagem é conhecida."""
    writers: dict[str, GlueJob] = {}
    # Primeiro a linhagem declarada nos argumentos do job.
    for job in account.glue_jobs:
        for written in job.writes_tables:
            writers.setdefault(written, job)
    # Depois a que o catálogo registra, sem sobrescrever a anterior.
    for table in account.tables:
        if not table.written_by:
            continue
        producer = account.job_by_name(table.written_by)
        if producer is not None:
            writers.setdefault(table.name, producer)
    return writers


def _wasted_production(
    account: Account,
    table: str,
    writer: GlueJob,
    query,
    config: Config,
    scan_id: str,
) -> Opportunity:
    production_cost = writer.monthly_dpu_hours * config.pricing.glue_rate(
        writer.execution_class
    )
    reason = (
        "varredura completa confirmada"
        if query.full_scan_confirmed
        else "sem filtro nas partições "
        + ", ".join(query.missing_partition_filters[:3])
    )
    return build(
        Finding(
            rule_id="XSVC-WASTED-PRODUCTION",
            rule_version="1.0.0",
            asset_type="table",
            asset_name=table,
            title="Tabela cara de produzir é lida de forma que desperdiça o esforço",
            why=(
                f"O job '{writer.name}' consome {writer.monthly_dpu_hours:.1f} DPU-hora "
                f"por mês para produzir esta tabela, e o padrão de leitura no Athena "
                f"tem {reason}. Particionar na origem só compensa se a leitura usar "
                "as partições."
            ),
            source_process=writer.name,
        ),
        Recommendation(
            difficulty=3,
            action="Alinhar o particionamento de escrita com o filtro de leitura",
            how_to_apply=(
                "Comparar as colunas de partição gravadas pelo job com os predicados "
                "que as queries realmente usam; ajustar o lado que estiver errado — "
                "às vezes é a escrita, às vezes é a query."
            ),
            how_to_validate=(
                "Comparar DPU-hora do job e bytes faturáveis da query na mesma janela, "
                "antes e depois. Os dois precisam cair, ou pelo menos um cair sem o "
                "outro subir."
            ),
            risks=[
                "mudar particionamento reescreve dados e afeta todos os consumidores",
                "outros leitores podem depender do layout atual",
            ],
            docs=[_DOC_PARTITION, _DOC_PUSHDOWN],
            blocked=True,
        ),
        Evidence(
            items=[
                f"produção: {writer.monthly_dpu_hours:.1f} DPU-hora/mês em '{writer.name}'",
                f"leitura: {reason}",
                f"{query.observed_runs} execuções observadas do padrão",
                "os dois lados precisam ser medidos juntos antes de mudar qualquer um",
            ],
            sources=["Glue GetJobRuns", "Athena GetQueryExecution", "Glue GetTable"],
            observed_runs=min(writer.observed_runs, query.observed_runs),
            coverage_days=min(writer.coverage_days, query.coverage_days),
            has_optional_metrics=False,
            owner_tag=writer.owner_tag,
        ),
        Estimation(
            method="cross_service_wasted_production_v1",
            baseline_cost=round(production_cost, 2),
            projected_cost=round(production_cost, 2),
            estimated_saving=0.0,
            assumptions=[
                "custo de produção é o baseline; quanto dele se recupera depende de "
                "qual lado será ajustado",
                "economia só é afirmável com benchmark dos dois lados",
            ],
            pricing_region=config.pricing.region,
            estimation_version=config.pricing.version,
            saving_quality="unavailable",
        ),
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
