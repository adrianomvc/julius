"""As fontes de coleta, declaradas como dado.

Cada fonte era um bloco de quinze linhas dentro de uma função de trezentas: a
chamada, o fallback, a contagem, o esperado, o impacto e a próxima ação, tudo
inline. Adicionar fonte significava inserir no meio e copiar o bloco de saúde
junto; propagar um parâmetro novo significava tocar catorze pontos idênticos.

Aqui uma fonte é uma linha de `SOURCES`. A ordem da lista é a ordem de
execução, e é significativa: o rateio de custo Glue precisa do inventário de
jobs, e o grafo de processos precisa dos schedules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from julius.collection.collectors import (
    cloudtrail,
    cloudwatch,
    cost_explorer,
    datawarm,
    redshift,
    redshift_cost,
    sagemaker,
    schedules,
    stepfunctions,
    touches,
)
from julius.collection.collectors.athena import monthly as athena
from julius.collection.collectors.glue import cost as glue_cost
from julius.collection.collectors.glue import crawlers as glue_crawlers
from julius.collection.collectors.glue import (
    databrew,
    jobs,
    sessions,
    spark_logs,
    triggers,
)
from julius.collection.health import CollectionRecorder
from julius.collection.models import Account, CollectionHealth
from julius.collection.scope import CatalogScope
from julius.collection.window import AnalysisWindow, BillingMonth


@dataclass
class CollectionContext:
    """O que toda fonte precisa saber, montado uma vez por scan."""

    session: Any
    window: AnalysisWindow
    billing: BillingMonth
    account: Account
    # Deliberadamente sem tipo: a coleta lê atributos nomeados da configuração
    # e da taxonomia de domínio, mas não conhece as classes que os definem —
    # é assim que a seta continua apontando só para baixo.
    config: Any
    glue_usage_markers: Sequence[tuple[str, str]] = ()
    allocatable_glue_buckets: frozenset[str] = frozenset()
    glue_cost_version: str = ""
    redshift_usage_markers: Sequence[tuple[str, str]] = ()
    redshift_compute_buckets: frozenset[str] = frozenset()
    redshift_cost_version: str = ""
    touches_table: str = ""
    athena_workgroup: str = "julius"
    athena_output: str | None = None
    include_cloudtrail: bool = False
    datawarm_job: str = ""
    # Qual recorte do Glue Catalog pertence a esta conta. O default vazio
    # mantém o comportamento antigo — todos os bancos — e diz isso na saúde.
    catalog_scope: CatalogScope = field(default_factory=CatalogScope)
    # Sinais que uma fonte deixa para a seguinte — o rateio de custo só se
    # considera reconciliado quando o inventário de jobs veio íntegro.
    flags: dict[str, Any] = field(default_factory=dict)
    # Entradas de saúde produzidas dentro de um coletor: o Athena consulta sete
    # dependências e cada uma vira fonte própria no relatório.
    pending_health: list[CollectionHealth] = field(default_factory=list)
    def client(self, service: str) -> Any:
        return self.session.client(service)


@dataclass(frozen=True)
class Source:
    """Uma fonte de coleta e tudo que a saúde precisa saber sobre ela."""

    name: str
    collect: Callable[[CollectionContext], Any]
    impact: str
    next_action: str
    required: bool = False
    # Onde o resultado aterrissa: um atributo do Account, ou uma função quando
    # a fonte enriquece algo já coletado em vez de devolver uma lista nova.
    into: str = ""
    apply: Callable[[CollectionContext, Any], None] | None = None
    default: Callable[[], Any] = list
    count: Callable[[Any], int] | None = None
    expected: Callable[[CollectionContext], int | None] | None = None
    data_through: Callable[[Any], str] | None = None
    # Fonte opcional: quando desligada, aparece na saúde com o motivo em vez de
    # sumir do relatório.
    enabled: Callable[[CollectionContext], bool] | None = None
    disabled_category: str = "not_configured"
    disabled_impact: str = ""
    disabled_next_action: str = ""
    disabled_affects_status: Callable[[CollectionContext], bool] = lambda ctx: False
    # Ajuste da entrada de saúde depois do fato — evidência parcial, por exemplo.
    after: Callable[[CollectionContext, Any, CollectionHealth], None] | None = None


def run(source: Source, ctx: CollectionContext, recorder: CollectionRecorder) -> None:
    """Executa uma fonte e registra a saúde dela."""
    if source.enabled is not None and not source.enabled(ctx):
        recorder.unavailable(
            source.name,
            category=source.disabled_category,
            impact=source.disabled_impact or source.impact,
            next_action=source.disabled_next_action or source.next_action,
            affects_status=source.disabled_affects_status(ctx),
        )
        return

    result = recorder.capture(
        source.name,
        lambda: source.collect(ctx),
        source.default(),
        required=source.required,
        count=source.count,
        expected=source.expected(ctx) if source.expected else None,
        data_through=source.data_through,
        impact=source.impact,
        next_action=source.next_action,
    )
    if source.into:
        setattr(ctx.account, source.into, result)
    if source.apply is not None:
        source.apply(ctx, result)
    if source.after is not None:
        source.after(ctx, result, recorder.entries[-1])
    if ctx.pending_health:
        recorder.entries.extend(ctx.pending_health)
        ctx.pending_health.clear()


def _latest_data_through(items: Any) -> str:
    values = [
        str(getattr(item, "data_through", "") or getattr(item, "window_end", ""))
        for item in items
    ]
    return max((value for value in values if value), default="")


def _enriched(fn: Callable[[], Any], value: Any) -> Any:
    """Coletor que enriquece em vez de devolver: a contagem olha o alvo."""
    fn()
    return value


# --------------------------------------------------------------------------
# Ajustes pós-coleta que não cabem num atributo
# --------------------------------------------------------------------------


def _record_jobs_integrity(
    ctx: CollectionContext, _result: Any, entry: CollectionHealth
) -> None:
    ctx.flags["jobs_collection_complete"] = entry.status == "ok"


def _flag_partial_spark_evidence(
    ctx: CollectionContext, result: Any, entry: CollectionHealth
) -> None:
    if any(
        job.spark_event_log_objects_scanned > 0
        and not job.spark_event_log_evidence_complete
        for job in result
    ):
        entry.status = "partial"
        entry.error_category = "bounded_or_incomplete"
        entry.impact = "shuffle e spill usam somente evidência parcial"
        entry.next_action = "revisar limites, objetos truncados e prefixo dos event logs"


def _apply_athena_analysis(ctx: CollectionContext, analysis: Any) -> None:
    if analysis is None:
        return
    ctx.account.athena_queries = analysis.queries
    ctx.account.athena_actor_usage = analysis.actors
    ctx.account.athena_coverage = analysis.coverage
    if analysis.coverage.cost_metric:
        ctx.account.currency = analysis.coverage.currency or ctx.account.currency


def _publish_athena_dependencies(
    ctx: CollectionContext, analysis: Any, _entry: CollectionHealth
) -> None:
    """As dependências internas do Athena entram na saúde como fontes próprias.

    Sem isso, uma falha de CloudTrail e uma permissão faltando no Glue Catalog
    ficavam indistinguíveis dentro de uma única linha chamada "Athena Queries".
    """
    if analysis is None:
        return
    ctx.pending_health.extend(analysis.health)


def _apply_touches(ctx: CollectionContext, stats: dict) -> None:
    for table in ctx.account.tables:
        measured = stats.get(table.name)
        if measured is None:
            continue
        table.touches_90d = measured.touches
        table.consuming_accounts = measured.accounts
        table.consuming_communities = measured.communities
        table.used_by_accounts = list(measured.account_ids)
        table.primary_community = measured.primary_community


def _event_log_jobs(ctx: CollectionContext) -> list:
    return [job for job in ctx.account.glue_jobs if job.spark_event_logs_path]


def _collect_catalog(ctx: CollectionContext) -> list:
    """Lista os bancos, aplica o escopo e só então lê tabelas.

    A ordem é o ponto: um `get_tables` por banco é o que custa, e num Data Mesh
    a maioria dos bancos do catálogo pertence a outras contas.
    """
    glue = ctx.client("glue")
    seen = jobs.list_database_names(glue)
    chosen = ctx.catalog_scope.select(seen)
    ctx.pending_health.append(_catalog_scope_health(ctx, seen, chosen))
    return jobs.collect_tables(glue, chosen)


def _catalog_scope_health(
    ctx: CollectionContext, seen: list[str], chosen: list[str]
) -> CollectionHealth:
    """Quantos bancos ficaram de fora, e por qual regra.

    Sem esta entrada a conta simplesmente mostra menos tabelas, e não há como
    distinguir escopo de permissão faltando — os dois se parecem com "sumiu".
    """
    scope = ctx.catalog_scope
    now = datetime.now(timezone.utc).isoformat()
    status = "ok"
    category = ""
    next_action = ""
    if not seen:
        # Catálogo vazio não é problema de escopo; a fonte em si já reporta.
        pass
    elif not scope.declared:
        status = "partial"
        category = "not_configured"
        next_action = (
            "informar --account-name para restringir o catálogo à conta analisada"
        )
    elif not chosen:
        status = "partial"
        category = "no_data"
        next_action = (
            f"nenhum banco casou com {scope.rule}; conferir o nome da conta "
            "ou informar --glue-databases"
        )
    return CollectionHealth(
        source="Glue Catalog Scope",
        status=status,
        started_at=now,
        completed_at=now,
        collected=len(chosen),
        expected=len(seen),
        coverage=round(len(chosen) / len(seen), 4) if seen else None,
        error_category=category,
        impact=scope.rule,
        next_action=next_action,
        affects_status=False,
    )


# --------------------------------------------------------------------------
# As fontes, na ordem em que rodam
# --------------------------------------------------------------------------

SOURCES: tuple[Source, ...] = (
    Source(
        name="Cost Explorer",
        collect=lambda ctx: cost_explorer.collect_services(
            ctx.client("ce"), billing=ctx.billing, include_forecast=True
        ),
        into="services",
        count=len,
        data_through=_latest_data_through,
        impact="cobrança MTD, forecast e reconciliação ficam indisponíveis",
        next_action="validar ce:GetCostAndUsage e ce:GetCostForecast",
        apply=lambda ctx, services: (
            setattr(ctx.account, "currency", services[0].currency) if services else None
        ),
    ),
    Source(
        name="Glue Jobs",
        collect=lambda ctx: jobs.collect_jobs(ctx.client("glue"), window=ctx.window),
        into="glue_jobs",
        required=True,
        count=len,
        data_through=_latest_data_through,
        impact="sem inventário de jobs o scan Glue não é confiável",
        next_action="validar glue:GetJobs e glue:GetJobRuns",
        after=_record_jobs_integrity,
    ),
    Source(
        name="Glue Scripts",
        collect=lambda ctx: None,
        enabled=lambda ctx: False,
        disabled_category="separate_stage_required",
        disabled_affects_status=lambda ctx: any(
            job.script_location for job in ctx.account.glue_jobs
        ),
        impact="análise estática de código ainda não foi executada",
        next_action="executar julius agent collect-artifacts e reutilizar o manifesto",
    ),
    Source(
        name="Spark Event Logs",
        collect=lambda ctx: _enriched(
            lambda: spark_logs.enrich_glue_shuffle(
                ctx.client("s3"), ctx.account.glue_jobs, window=ctx.window
            ),
            _event_log_jobs(ctx),
        ),
        count=lambda items: sum(
            1 for job in items if job.spark_event_log_objects_scanned > 0
        ),
        expected=lambda ctx: len(_event_log_jobs(ctx)),
        enabled=lambda ctx: bool(_event_log_jobs(ctx)),
        disabled_affects_status=lambda ctx: bool(ctx.account.glue_jobs),
        disabled_impact="shuffle e spill não podem ser confirmados por logs",
        disabled_next_action="configurar --spark-event-logs-path nos jobs relevantes",
        impact="shuffle, spill e evidência de código permanecem investigações",
        next_action="validar s3:GetObject e o --spark-event-logs-path dos jobs",
        after=_flag_partial_spark_evidence,
    ),
    Source(
        name="Glue Catalog",
        collect=_collect_catalog,
        into="tables",
        count=len,
        impact="linhagem e oportunidades de tabelas ficam incompletas",
        next_action="validar glue:GetDatabases e glue:GetTables",
    ),
    Source(
        name="Glue Crawlers",
        collect=lambda ctx: glue_crawlers.collect_crawlers(
            ctx.client("glue"), window=ctx.window
        ),
        into="glue_crawlers",
        count=len,
        data_through=_latest_data_through,
        impact="falhas, recrawl e schedules de crawlers não são avaliados",
        next_action="validar glue:GetCrawlers, glue:GetCrawlerMetrics e glue:ListCrawls",
    ),
    Source(
        name="Glue Triggers",
        collect=lambda ctx: triggers.collect_triggers(ctx.client("glue")),
        into="glue_triggers",
        count=len,
        impact="frequência e grafo de processos podem ficar incompletos",
        next_action="validar glue:GetTriggers",
    ),
    Source(
        name="Glue DataBrew",
        collect=lambda ctx: databrew.collect_jobs(
            ctx.client("databrew"), window=ctx.window
        ),
        into="databrew_jobs",
        count=len,
        data_through=_latest_data_through,
        impact="custos e falhas do DataBrew não são avaliados",
        next_action="validar permissões read-only do DataBrew",
    ),
    Source(
        name="CloudWatch Glue CPU",
        collect=lambda ctx: _enriched(
            lambda: cloudwatch.enrich_glue_cpu(
                ctx.client("cloudwatch"), ctx.account.glue_jobs, window=ctx.window
            ),
            ctx.account.glue_jobs,
        ),
        count=lambda items: sum(job.avg_cpu_load is not None for job in items),
        expected=lambda ctx: len(ctx.account.glue_jobs),
        impact="recomendações de capacidade permanecem bloqueadas",
        next_action="habilitar métricas Glue e validar cloudwatch:GetMetricStatistics",
    ),
    Source(
        name="CloudWatch Glue Observability",
        collect=lambda ctx: _enriched(
            lambda: cloudwatch.enrich_glue_observability(
                ctx.client("cloudwatch"), ctx.account.glue_jobs, window=ctx.window
            ),
            ctx.account.glue_jobs,
        ),
        count=lambda items: sum(
            job.avg_worker_utilization is not None
            or job.max_memory_used_pct is not None
            or job.max_disk_used_pct is not None
            or job.max_task_skew is not None
            for job in items
        ),
        expected=lambda ctx: len(ctx.account.glue_jobs),
        impact="memória, disco, skew e executor gap ficam incompletos",
        next_action="habilitar Glue Observability e validar métricas no CloudWatch",
    ),
    Source(
        name="Glue Interactive Sessions",
        collect=lambda ctx: sessions.collect_sessions(
            ctx.client("glue"), window=ctx.window
        ),
        into="interactive_sessions",
        count=len,
        data_through=_latest_data_through,
        impact="ociosidade e capacidade de sessões não são avaliadas",
        next_action="validar glue:ListSessions e glue:ListStatements",
    ),
    Source(
        # Depende de jobs, crawlers, sessions e DataBrew já estarem no inventário.
        name="Glue Cost Explorer",
        collect=lambda ctx: glue_cost.allocate_costs(
            ctx.account,
            glue_cost.collect_glue_costs(
                ctx.client("ce"),
                window=ctx.window,
                markers=ctx.glue_usage_markers,
                version=ctx.glue_cost_version,
            ),
            ctx.config,
            allocatable_buckets=ctx.allocatable_glue_buckets,
            jobs_collection_complete=ctx.flags.get("jobs_collection_complete", False),
        ),
        into="glue_cost_coverage",
        default=lambda: None,
        count=lambda coverage: 1 if coverage and coverage.buckets else 0,
        # Só é esperado haver cobrança quando existe job para atribuir.
        expected=lambda ctx: 1 if ctx.account.glue_jobs else 0,
        data_through=lambda coverage: coverage.data_through if coverage else "",
        impact="custo Glue permanece modelado por tarifa, sem âncora na fatura",
        next_action="validar ce:GetCostAndUsage com GroupBy USAGE_TYPE",
    ),
    Source(
        name="Athena Queries",
        collect=lambda ctx: athena.collect_analysis(
            ctx.client("athena"),
            cloudwatch_client=ctx.client("cloudwatch"),
            cloudtrail_client=ctx.client("cloudtrail"),
            identitystore_client=ctx.client("identitystore"),
            glue_client=ctx.client("glue"),
            s3_client=ctx.client("s3"),
            ce_client=ctx.client("ce"),
            window=ctx.window,
        ),
        default=lambda: None,
        apply=_apply_athena_analysis,
        after=_publish_athena_dependencies,
        count=lambda analysis: len(analysis.queries) if analysis else 0,
        impact="linhagem de leitura e oportunidades Athena ficam incompletas",
        next_action="validar permissões read-only do Athena",
    ),
    Source(
        name="SageMaker Studio",
        collect=lambda ctx: sagemaker.collect_apps(
            ctx.client("sagemaker"), ctx.client("cloudwatch"), window=ctx.window
        ),
        into="sagemaker_apps",
        count=len,
        impact=(
            "apps ociosos do Studio não são avaliados; sem DescribeApp/Space/Domain "
            "o idle shutdown fica desconhecido e a regra não dispara"
        ),
        next_action=(
            "validar sagemaker:ListApps, DescribeApp/DescribeSpace/DescribeDomain "
            "e métricas do namespace Studio"
        ),
    ),
    Source(
        name="SageMaker Endpoints",
        collect=lambda ctx: sagemaker.collect_endpoints(
            ctx.client("sagemaker"),
            ctx.client("cloudwatch"),
            ctx.client("application-autoscaling"),
            window=ctx.window,
        ),
        into="sagemaker_endpoints",
        count=len,
        impact="endpoints sem uso não são avaliados",
        next_action=(
            "validar sagemaker:ListEndpoints, DescribeEndpoint e "
            "application-autoscaling:DescribeScalableTargets"
        ),
    ),
    Source(
        name="Amazon Redshift",
        collect=lambda ctx: redshift.collect_clusters(
            ctx.client("redshift"),
            ctx.client("cloudwatch"),
            ctx.client("redshift-serverless"),
            window=ctx.window,
        ),
        into="redshift_clusters",
        count=len,
        impact="capacidade e ociosidade de Redshift não são avaliadas",
        next_action="validar redshift:DescribeClusters e redshift-serverless:ListWorkgroups",
    ),
    Source(
        # Depende do inventário de clusters para ratear o compute.
        name="Redshift Cost Explorer",
        collect=lambda ctx: redshift_cost.allocate_costs(
            ctx.account,
            redshift_cost.collect_redshift_costs(
                ctx.client("ce"),
                window=ctx.window,
                markers=ctx.redshift_usage_markers,
                version=ctx.redshift_cost_version,
            ),
            ctx.redshift_compute_buckets,
        ),
        into="redshift_cost_coverage",
        default=lambda: None,
        count=lambda coverage: 1 if coverage and coverage.buckets else 0,
        expected=lambda ctx: 1 if getattr(ctx.account, "redshift_clusters", None) else 0,
        data_through=lambda coverage: coverage.data_through if coverage else "",
        impact=(
            "sem cobrança rateada o cluster ocioso continua sendo investigação "
            "sem economia quantificada"
        ),
        next_action="validar ce:GetCostAndUsage com GroupBy USAGE_TYPE",
    ),
    Source(
        name="Step Functions",
        collect=lambda ctx: stepfunctions.collect_state_machines(
            ctx.client("stepfunctions"), window=ctx.window
        ),
        into="state_machines",
        count=len,
        impact=(
            "grafo de processos e frequência podem ficar incompletos; sem "
            "GetExecutionHistory as transições não são contadas e as regras "
            "Standard→Express e polling não quantificam economia"
        ),
        next_action=(
            "validar states:ListStateMachines, DescribeStateMachine e "
            "GetExecutionHistory read-only"
        ),
    ),
    Source(
        name="EventBridge Schedules",
        collect=lambda ctx: schedules.collect_schedules(ctx.client("events")),
        into="schedules",
        count=len,
        impact="frequência esperada dos processos pode ficar incompleta",
        next_action="validar events:ListRules e events:ListTargetsByRule",
    ),
    Source(
        name="Table Touches",
        collect=lambda ctx: touches.collect_touches(
            ctx.client("athena"),
            touches_table=ctx.touches_table,
            workgroup=ctx.athena_workgroup,
            output_location=ctx.athena_output,
            window=ctx.window,
        ),
        default=dict,
        apply=_apply_touches,
        count=len,
        enabled=lambda ctx: bool(ctx.touches_table),
        disabled_impact="uso e consumidores das tabelas não são avaliados",
        disabled_next_action="informar --touches-table para habilitar a fonte",
        impact="uso e consumidores das tabelas ficam incompletos",
        next_action="validar tabela, workgroup e saída Athena configurados",
    ),
    Source(
        name="DataWarm Mapping",
        collect=lambda ctx: _enriched(
            lambda: datawarm.mark_publications(ctx.account, ctx.datawarm_job),
            ctx.account.tables,
        ),
        count=lambda tables: sum(table.datawarm_published for table in tables),
        enabled=lambda ctx: bool(ctx.datawarm_job),
        disabled_category="not_enabled",
        disabled_impact="publicações DataWarm não são classificadas",
        disabled_next_action="usar --datawarm-job quando essa governança for necessária",
        impact="publicações DataWarm podem não ser reconhecidas",
        next_action="validar o identificador informado em --datawarm-job",
    ),
    Source(
        name="CloudTrail Ownership",
        collect=lambda ctx: cloudtrail.collect_actor_events(
            ctx.client("cloudtrail"), window=ctx.window
        ),
        into="actor_events",
        count=len,
        enabled=lambda ctx: ctx.include_cloudtrail,
        disabled_category="not_enabled",
        disabled_impact="responsáveis dependem de tags e fallbacks existentes",
        disabled_next_action=(
            "usar --cloudtrail quando a atribuição de autoria for necessária"
        ),
        impact="responsáveis inferidos podem permanecer desconhecidos",
        next_action="validar cloudtrail:LookupEvents",
    ),
)
