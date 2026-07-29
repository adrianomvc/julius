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
    s3,
    s3_access,
    s3_config,
    s3_cost,
    sagemaker,
    sagemaker_cost,
    sagemaker_extended,
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
from julius.collection.collectors.s3_evidence import MAX_LIST_PAGES
from julius.collection.health import CollectionRecorder
from julius.collection.models import Account, CollectionHealth
from julius.collection.scope import CatalogScope
from julius.collection.session import S3_LISTING_WORKERS, make_client
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
    sagemaker_usage_markers: Sequence[tuple[str, str]] = ()
    allocatable_sagemaker_buckets: frozenset[str] = frozenset()
    sagemaker_cost_version: str = ""
    touches_table: str = ""
    athena_workgroup: str = "julius"
    athena_output: str | None = None
    include_cloudtrail: bool = False
    datawarm_job: str = ""
    # Qual recorte do Glue Catalog pertence a esta conta. O default vazio
    # mantém o comportamento antigo — todos os bancos — e diz isso na saúde.
    catalog_scope: CatalogScope = field(default_factory=CatalogScope)
    # Lista os prefixos S3 até o fim, em paralelo, em vez de parar no teto de
    # páginas. Desligado por default porque cobra request: a coleta limitada
    # responde "pelo menos X GB", e é o operador que decide pagar pela resposta
    # exata. Ver `S3 Prefixes` em `SOURCES`.
    s3_full_listing: bool = False
    # Inventário de jobs é sempre completo; esta opção remove o limite de 100
    # jobs com métricas CloudWatch detalhadas.
    sagemaker_full_metrics: bool = False
    # Sinais que uma fonte deixa para a seguinte — o rateio de custo só se
    # considera reconciliado quando o inventário de jobs veio íntegro.
    flags: dict[str, Any] = field(default_factory=dict)
    # Entradas de saúde produzidas dentro de um coletor: o Athena consulta sete
    # dependências e cada uma vira fonte própria no relatório.
    pending_health: list[CollectionHealth] = field(default_factory=list)
    # Onde um coletor anota a chamada interna que não conseguiu fazer. `run`
    # limpa antes de cada fonte e lê depois, então o coletor só precisa receber
    # `gaps=ctx.gaps` e nunca sabe o nome da fonte em que está.
    gaps: list[str] = field(default_factory=list)
    _clients: dict[str, Any] = field(default_factory=dict, repr=False)

    def client(self, service: str) -> Any:
        """Um cliente por serviço, criado uma vez.

        Cada `session.client(...)` lê e monta o modelo do serviço. A fonte do
        Athena sozinha pedia sete clientes, e `glue` era reconstruído em cinco
        fontes diferentes — trabalho repetido antes da primeira chamada AWS.
        """
        if service not in self._clients:
            self._clients[service] = make_client(self.session, service)
        return self._clients[service]


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

    ctx.gaps.clear()
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
    _record_gaps(ctx, recorder.entries[-1])
    if ctx.pending_health:
        recorder.entries.extend(ctx.pending_health)
        ctx.pending_health.clear()


#: Ordem de gravidade: a categoria mais acionável é a que a fonte reporta.
_GRAVIDADE = ("permission_denied", "credentials_expired", "throttled", "not_found")


def _record_gaps(ctx: CollectionContext, entry: CollectionHealth) -> None:
    """Uma listagem interna negada degrada a fonte, em vez de virar zero.

    Um crawler sem `ListCrawls`, um banco sem `GetTables`, uma máquina sem
    `DescribeStateMachine`: a fonte continua entregando o que leu, e a
    degradação precisa aparecer — senão os zeros do que não foi lido passam por
    medição, e o relatório afirma que o serviço não é usado nesta conta.

    Só escala: uma fonte que o `after` da própria fonte já marcou como parcial
    não volta a `ok` por aqui.
    """
    if not ctx.gaps:
        return
    anotados = sorted(set(ctx.gaps))
    categorias = {gap.rsplit(": ", 1)[-1] for gap in anotados}
    if entry.status == "ok":
        entry.status = "partial" if entry.collected else "unavailable"
    if not entry.error_category:
        entry.error_category = next(
            (nome for nome in _GRAVIDADE if nome in categorias),
            "bounded_or_incomplete",
        )
    faltou = "não lido: " + "; ".join(anotados)
    entry.impact = f"{entry.impact} — {faltou}" if entry.impact else faltou


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
    ctx: CollectionContext, result: Any, entry: CollectionHealth
) -> None:
    # Um job cujo histórico foi negado entra no inventário com a configuração
    # que o `GetJobs` trouxe, e sem execuções. Antes esse job derrubava a fonte
    # obrigatória e a conta inteira ficava sem scan; agora ele degrada a fonte,
    # e a degradação precisa aparecer — senão os zeros dele passam por medição.
    sem_historico = [job.name for job in result if not job.run_history_available]
    if sem_historico:
        entry.status = "partial"
        entry.error_category = "permission_denied"
        entry.impact = (
            f"{len(sem_historico)} job(s) sem histórico de execução: duração, "
            "recorrência, taxa de falha e DPU-hora deles não foram medidos"
        )
        entry.next_action = (
            "validar glue:GetJobRuns nesses jobs — política de recurso, "
            "Lake Formation ou tag de restrição"
        )
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
        # A tabela oficial de toques é a melhor fonte de leitura que existe
        # nesta conta; ela sobrescreve o que o histórico do Athena derivaria.
        if measured.last_access:
            table.last_read_at = measured.last_access


def _event_log_jobs(ctx: CollectionContext) -> list:
    return [job for job in ctx.account.glue_jobs if job.spark_event_logs_path]


def _s3_scope(ctx: CollectionContext) -> list[tuple[str, str, str]]:
    """O escopo de S3, derivado do inventário já coletado, e memorizado.

    Três fontes precisam da mesma lista, e ela depende de tabelas, jobs e
    workgroups — por isso as fontes de S3 rodam depois deles.
    """
    if "s3_prefixes" not in ctx.flags:
        ctx.flags["s3_prefixes"] = s3.known_prefixes(ctx.account)
    return ctx.flags["s3_prefixes"]


def _collect_s3_prefixes(ctx: CollectionContext) -> list:
    """Um `collect_prefixes` por tipo: o limiar de obsolescência é por tipo.

    Resultado de query vence em um dia, event log em trinta, staging em sete.
    Uma chamada só com um limiar médio marcaria como velho o que não é e
    perderia o que é.
    """
    thresholds = ctx.config.thresholds
    por_tipo = {
        "athena_results": thresholds.s3_athena_results_stale_days,
        "spark_logs": thresholds.s3_spark_logs_stale_days,
        "staging": thresholds.s3_staging_stale_days,
        "table_location": thresholds.s3_spark_logs_stale_days,
    }
    client = ctx.client("s3")
    out: list = []
    for kind, stale_after in por_tipo.items():
        conhecidos = [item for item in _s3_scope(ctx) if item[1] == kind]
        if not conhecidos:
            continue
        out.extend(
            s3.collect_prefixes(
                client,
                known=conhecidos,
                window=ctx.window,
                stale_after_days=stale_after,
                max_pages=None if ctx.s3_full_listing else MAX_LIST_PAGES,
                workers=S3_LISTING_WORKERS if ctx.s3_full_listing else 1,
            )
        )
    return out


def _flag_buckets_without_access_evidence(
    ctx: CollectionContext, result: Any, entry: CollectionHealth
) -> None:
    """Bucket sem fonte de último acesso limita o que a análise pode afirmar.

    Não é erro de coleta — é o estado real da conta, e precisa aparecer como
    tal: sem server access logs, Storage Lens, Storage Class Analysis ou
    Intelligent-Tiering, o Julius conhece a última **escrita** de cada objeto e
    mais nada. Recomendar Glacier a partir disso trocaria a classe de um arquivo
    gravado uma vez e lido todo dia.
    """
    sem_evidencia = s3_config.buckets_without_access_evidence(result)
    if not sem_evidencia:
        return
    if entry.status == "ok":
        entry.status = "partial"
    entry.error_category = entry.error_category or "bounded_or_incomplete"
    entry.impact = (
        f"{len(sem_evidencia)} bucket(s) sem fonte de último acesso: "
        "transição de classe sai como sinal, não como economia"
    )
    entry.next_action = (
        "habilitar server access logging (s3:PutBucketLogging pelo time dono) "
        "ou Storage Lens advanced nesses buckets"
    )


def _flag_partial_s3_listing(
    ctx: CollectionContext, result: Any, entry: CollectionHealth
) -> None:
    """Prefixo listado até o teto não pode passar por prefixo listado inteiro."""
    if any(not item.listing_complete for item in result):
        entry.status = "partial"
        entry.error_category = "bounded_or_incomplete"
        entry.impact = "objetos antigos contados somente sobre a parte listada"
        entry.next_action = "revisar os prefixos truncados antes de agir sobre volume"
    _record_listing_cost(ctx, result, entry)


def _record_listing_cost(
    ctx: CollectionContext, result: Any, entry: CollectionHealth
) -> None:
    """Quanto a própria coleta gastou em `ListObjectsV2`.

    O desenho original evita `ListBuckets` porque listar um data lake cobra por
    request, e o produto não pode custar dinheiro para descobrir custo. A
    listagem completa contraria isso de propósito, a pedido — então ela declara
    o que gastou, em vez de a conta aparecer na fatura do mês seguinte sem
    ninguém saber de onde veio.
    """
    requests = sum(int(getattr(item, "list_requests", 0) or 0) for item in result)
    if not requests:
        return
    entry.impact = _com_custo(entry.impact, requests, ctx)


def _com_custo(impacto: str, requests: int, ctx: CollectionContext) -> str:
    nota = f"{requests} requests de listagem"
    tarifa = getattr(ctx.config.pricing, "s3_request_per_1000", {}).get("list")
    if tarifa:
        nota += f" (~US$ {tarifa * requests / 1000.0:.2f})"
    else:
        # Sem tarifa na tabela, a contagem sozinha ainda diz o que foi gasto.
        nota += " (tarifa de LIST ausente: rode `julius pricing refresh`)"
    return f"{impacto} — {nota}" if impacto else nota


def _collect_catalog(ctx: CollectionContext) -> list:
    """Lista os bancos, aplica o escopo e só então lê tabelas.

    A ordem é o ponto: um `get_tables` por banco é o que custa, e num Data Mesh
    a maioria dos bancos do catálogo pertence a outras contas.
    """
    glue = ctx.client("glue")
    seen = jobs.list_database_names(glue)
    chosen = ctx.catalog_scope.select(seen)
    ctx.pending_health.append(_catalog_scope_health(ctx, seen, chosen))
    return jobs.collect_tables(glue, chosen, gaps=ctx.gaps)


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
            "cadastrar o perfil em ~/.julius-accounts.json ou informar "
            "--account-name para restringir o catálogo"
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
        collect=lambda ctx: jobs.collect_jobs(
            ctx.client("glue"), window=ctx.window, gaps=ctx.gaps
        ),
        into="glue_jobs",
        # Já foi obrigatória, e um `GetJobs` negado abortava a conta inteira —
        # S3, Athena, Redshift, SageMaker e Step Functions deixavam de ser
        # coletados por causa de uma permissão de outro serviço. Sem inventário
        # de jobs o **Glue** não é analisado; o resto do scan continua válido, e
        # `collection_status` cai para `partial` para dizer isso.
        count=len,
        data_through=_latest_data_through,
        impact="sem inventário de jobs o Glue não é analisado; demais serviços seguem",
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
            ctx.client("glue"), window=ctx.window, gaps=ctx.gaps
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
            ctx.client("databrew"), window=ctx.window, gaps=ctx.gaps
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
            ctx.client("glue"),
            window=ctx.window,
            account_id=ctx.account.account_id,
            gaps=ctx.gaps,
        ),
        into="interactive_sessions",
        count=len,
        data_through=_latest_data_through,
        impact="ociosidade, capacidade e responsável das sessões podem ficar incompletos",
        next_action="validar glue:ListSessions, glue:ListStatements e glue:GetTags",
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
        # Depende de tabelas, jobs e workgroups: o escopo de S3 é derivado do
        # inventário, nunca descoberto com ListBuckets.
        name="Amazon S3",
        collect=lambda ctx: s3.collect_buckets(
            ctx.client("cloudwatch"),
            ctx.client("s3"),
            names=s3.bucket_names(_s3_scope(ctx)),
            window=ctx.window,
        ),
        into="s3_buckets",
        count=len,
        enabled=lambda ctx: bool(_s3_scope(ctx)),
        disabled_category="no_data",
        disabled_impact="tamanho e composição dos buckets não são avaliados",
        disabled_next_action=(
            "coletar catálogo, jobs ou workgroups — o escopo de S3 sai deles"
        ),
        impact="tamanho e versionamento dos buckets ficam desconhecidos",
        next_action="validar cloudwatch:GetMetricStatistics e s3:GetBucketVersioning",
    ),
    Source(
        # Depende de "Amazon S3": a lista de buckets é a mesma.
        name="S3 Config",
        collect=lambda ctx: s3_config.collect_bucket_configs(
            ctx.client("s3"),
            names=s3.bucket_names(_s3_scope(ctx)),
            s3control_client=ctx.client("s3control"),
            account_id=ctx.account.account_id,
            gaps=ctx.gaps,
        ),
        into="s3_bucket_configs",
        count=len,
        enabled=lambda ctx: bool(_s3_scope(ctx)),
        disabled_category="no_data",
        disabled_impact="não se sabe quais buckets permitem medir último acesso",
        disabled_next_action=(
            "coletar catálogo, jobs ou workgroups — o escopo de S3 sai deles"
        ),
        # O S3 não tem last access time nativo por objeto: `LastModified` é a
        # última escrita. Sem saber o que está ligado no bucket, recomendar
        # classe fria seria apostar que arquivo não regravado não é lido.
        impact="recomendação de classe de armazenamento fica sem evidência de leitura",
        next_action=(
            "validar s3:GetBucketLogging, s3:GetLifecycleConfiguration, "
            "s3:GetAnalyticsConfiguration e s3:GetIntelligentTieringConfiguration"
        ),
        after=_flag_buckets_without_access_evidence,
    ),
    Source(
        name="S3 Prefixes",
        collect=_collect_s3_prefixes,
        into="s3_prefixes",
        count=len,
        enabled=lambda ctx: bool(_s3_scope(ctx)),
        disabled_category="no_data",
        disabled_impact="resultados, event logs e staging acumulados não são avaliados",
        disabled_next_action=(
            "coletar catálogo, jobs ou workgroups — o escopo de S3 sai deles"
        ),
        impact="acúmulo em prefixos conhecidos permanece invisível",
        next_action="validar s3:ListBucket nos prefixos do inventário",
        after=_flag_partial_s3_listing,
    ),
    Source(
        name="S3 Access Evidence",
        collect=lambda ctx: s3_access.collect_access_evidence(
            ctx.client("s3"),
            prefixes=ctx.account.s3_prefixes,
            configs=ctx.account.s3_bucket_configs,
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        count=len,
        enabled=lambda ctx: any(
            item.access_logging_enabled and item.access_log_target_bucket
            for item in ctx.account.s3_bucket_configs
        ),
        disabled_category="not_configured",
        disabled_impact=(
            "leituras fora do Athena não entram na decisão de classe S3"
        ),
        disabled_next_action=(
            "usar histórico/tabela de toques ou server access logs já habilitados"
        ),
        impact="recomendação de classe S3 fica sem leitura observada no bucket",
        next_action="validar s3:ListBucket e s3:GetObject no bucket de access logs",
    ),
    Source(
        name="S3 Multipart Uploads",
        collect=lambda ctx: s3.collect_multipart_uploads(
            ctx.client("s3"),
            names=s3.bucket_names(_s3_scope(ctx)),
            window=ctx.window,
        ),
        into="s3_multipart",
        count=len,
        enabled=lambda ctx: bool(_s3_scope(ctx)),
        disabled_category="no_data",
        disabled_impact="uploads abandonados não são avaliados",
        disabled_next_action=(
            "coletar catálogo, jobs ou workgroups — o escopo de S3 sai deles"
        ),
        impact=(
            "uploads iniciados e nunca concluídos continuam cobrando sem aparecer "
            "em nenhuma listagem de objetos"
        ),
        next_action="validar s3:ListMultipartUploads e s3:ListParts",
    ),
    Source(
        # Depende do inventário de buckets para ratear o armazenamento.
        name="S3 Cost Explorer",
        collect=lambda ctx: s3_cost.allocate_costs(
            ctx.account,
            s3_cost.collect_s3_costs(
                ctx.client("ce"),
                window=ctx.window,
                markers=ctx.config.s3_cost.usage_type_markers,
                version=ctx.config.s3_cost.version,
            ),
            ctx.config.s3_cost.storage_buckets,
        ),
        into="s3_cost_coverage",
        default=lambda: None,
        count=lambda coverage: 1 if coverage and coverage.buckets else 0,
        expected=lambda ctx: 1 if ctx.account.s3_buckets else 0,
        data_through=lambda coverage: coverage.data_through if coverage else "",
        impact="economia de exclusão fica sem cobrança real para ancorar",
        next_action="validar ce:GetCostAndUsage com GroupBy USAGE_TYPE",
    ),
    Source(
        name="SageMaker Studio",
        collect=lambda ctx: sagemaker.collect_apps(
            ctx.client("sagemaker"),
            ctx.client("cloudwatch"),
            window=ctx.window,
            gaps=ctx.gaps,
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
        name="SageMaker Spaces",
        collect=lambda ctx: sagemaker_extended.collect_spaces(
            ctx.client("sagemaker"),
            apps=ctx.account.sagemaker_apps,
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_spaces",
        count=len,
        impact="Spaces sem apps e storage EBS persistente não são inventariados",
        next_action="validar ListSpaces e DescribeSpace",
    ),
    Source(
        name="SageMaker Domains",
        collect=lambda ctx: sagemaker_extended.collect_domains(
            ctx.client("sagemaker"),
            ctx.client("cloudwatch"),
            spaces=ctx.account.sagemaker_spaces,
            apps=ctx.account.sagemaker_apps,
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_domains",
        count=len,
        impact="Domains e storage EFS sem atividade não são avaliados",
        next_action="validar ListDomains, DescribeDomain e métricas AWS/EFS",
    ),
    Source(
        name="SageMaker Endpoints",
        collect=lambda ctx: sagemaker.collect_endpoints(
            ctx.client("sagemaker"),
            ctx.client("cloudwatch"),
            ctx.client("application-autoscaling"),
            window=ctx.window,
            gaps=ctx.gaps,
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
        name="SageMaker Notebooks",
        collect=lambda ctx: sagemaker_extended.collect_notebooks(
            ctx.client("sagemaker"),
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_notebooks",
        count=len,
        impact="notebooks clássicos ligados não aparecem nem como sinal contextual",
        next_action="validar sagemaker:ListNotebookInstances/DescribeNotebookInstance",
    ),
    Source(
        name="SageMaker Jobs",
        collect=lambda ctx: sagemaker_extended.collect_jobs(
            ctx.client("sagemaker"),
            ctx.client("cloudwatch"),
            window=ctx.window,
            pricing=ctx.config.pricing,
            detailed_limit=100,
            full_metrics=ctx.sagemaker_full_metrics,
            history_days=90,
            low_utilization_threshold=ctx.config.thresholds.sm_low_utilization,
            gaps=ctx.gaps,
        ),
        into="sagemaker_jobs",
        count=len,
        impact=(
            "custos falhos, Spot, warm pools e dimensionamento de training, "
            "processing e transform ficam sem evidência"
        ),
        next_action=(
            "validar List/DescribeTrainingJobs, ProcessingJobs e TransformJobs "
            "e CloudWatch GetMetricData"
        ),
    ),
    Source(
        name="SageMaker Feature Store",
        collect=lambda ctx: sagemaker_extended.collect_feature_groups(
            ctx.client("sagemaker"),
            ctx.client("cloudwatch"),
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_feature_groups",
        count=len,
        impact="throughput provisionado e saúde do Feature Store não são avaliados",
        next_action="validar List/DescribeFeatureGroup e métricas Feature Store",
    ),
    Source(
        name="SageMaker Pipelines",
        collect=lambda ctx: sagemaker_extended.collect_pipelines(
            ctx.client("sagemaker"),
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_pipelines",
        count=len,
        impact="falhas e reprocessamentos não são agrupados por pipeline",
        next_action="validar List/DescribePipeline e histórico de execuções/passos",
    ),
    Source(
        name="SageMaker Model Monitor",
        collect=lambda ctx: sagemaker_extended.collect_monitoring_schedules(
            ctx.client("sagemaker"),
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_monitoring_schedules",
        count=len,
        impact="falhas dos schedules existentes de Model Monitor não são sinalizadas",
        next_action="validar List/DescribeMonitoringSchedule",
    ),
    Source(
        name="SageMaker Inference Recommender",
        collect=lambda ctx: sagemaker_extended.collect_inference_recommendations(
            ctx.client("sagemaker"),
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="sagemaker_inference_recommendations",
        count=len,
        impact="não há alvo AWS validado para comparar custo e desempenho do endpoint",
        next_action="validar List/DescribeInferenceRecommendationsJob",
    ),
    Source(
        name="SageMaker Cost Explorer",
        collect=lambda ctx: sagemaker_cost.collect_and_allocate_costs(
            ctx.account,
            ctx.client("ce"),
            window=ctx.window,
            markers=ctx.sagemaker_usage_markers,
            version=ctx.sagemaker_cost_version,
            allocatable=ctx.allocatable_sagemaker_buckets,
        ),
        into="sagemaker_cost_coverage",
        default=lambda: None,
        count=lambda coverage: 1 if coverage and coverage.buckets else 0,
        expected=lambda ctx: 1
        if (
            ctx.account.sagemaker_apps
            or ctx.account.sagemaker_endpoints
            or ctx.account.sagemaker_jobs
            or ctx.account.sagemaker_feature_groups
            or ctx.account.sagemaker_spaces
            or ctx.account.sagemaker_domains
        )
        else 0,
        data_through=lambda coverage: coverage.data_through if coverage else "",
        impact="economias SageMaker ficam sem teto da cobrança real",
        next_action="validar ce:GetCostAndUsage agrupado por USAGE_TYPE",
    ),
    Source(
        name="SageMaker Savings Plans",
        collect=lambda ctx: sagemaker_cost.collect_savings_plan_signal(
            ctx.client("ce"), window=ctx.window
        ),
        into="sagemaker_savings_plans",
        default=lambda: None,
        count=lambda signal: 1 if signal and signal.quality != "unavailable" else 0,
        impact="FinOps fica sem cobertura, utilização e recomendação de compromisso",
        next_action=(
            "validar GetSavingsPlansCoverage, Utilization e PurchaseRecommendation"
        ),
    ),
    Source(
        name="Amazon Redshift",
        collect=lambda ctx: redshift.collect_clusters(
            ctx.client("redshift"),
            ctx.client("cloudwatch"),
            ctx.client("redshift-serverless"),
            window=ctx.window,
            gaps=ctx.gaps,
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
            ctx.client("stepfunctions"), window=ctx.window, gaps=ctx.gaps
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
        collect=lambda ctx: schedules.collect_schedules(
            ctx.client("events"), gaps=ctx.gaps
        ),
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
            ctx.client("cloudtrail"), window=ctx.window, gaps=ctx.gaps
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
