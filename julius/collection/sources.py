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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from julius.collection.collectors import (
    billing_matrix,
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
    s3_inventory,
    sagemaker,
    sagemaker_cost,
    sagemaker_extended,
    schedules,
    stepfunctions,
    touches,
)
from julius.collection.collectors.athena import capacity as athena_capacity
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
from julius.collection.collectors.metrics import MetricBatchCoordinator
from julius.collection.collectors.s3_evidence import MAX_LIST_PAGES, parse_location
from julius.collection.health import CollectionRecorder, RequiredCollectionError
from julius.collection.iam import gaps_from_text
from julius.collection.models import (
    Account,
    CollectionHealth,
    GlueTrigger,
    IamGap,
    S3BucketConfig,
    Schedule,
)
from julius.collection.policy import ScopePolicy, policy_for_profile
from julius.collection.scope import CatalogScope
from julius.collection.session import S3_LISTING_WORKERS, make_client
from julius.collection.settings import retention_ceiling
from julius.collection.snapshot import CollectionSnapshotStore, SnapshotPolicy
from julius.collection.telemetry import InstrumentedClient, RunTelemetry
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
    scope_policy: ScopePolicy = field(default_factory=lambda: policy_for_profile(None))
    telemetry: RunTelemetry = field(default_factory=RunTelemetry)
    max_scan_cost_usd: float | None = None
    # Backpressure entre paginadores concorrentes. O limite controla páginas
    # entregues aos coletores, sem materializar filas de resposta em memória.
    max_parallel_pages: int = 8
    # Limite cooperativo e opt-in. Zero/None apenas mede o pico sem interromper.
    max_memory_mb: int | None = None
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
    athena_history_workgroups: tuple[str, ...] = ()
    athena_workgroup_roles: dict[str, str] = field(default_factory=dict)
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
    # Consome somente S3 Inventory já configurado; incompatibilidade mantém o
    # fallback atual por ListObjectsV2.
    s3_inventory: bool = False
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
    iam_gaps: dict[tuple[str, str], IamGap] = field(default_factory=dict)
    _clients: dict[str, Any] = field(default_factory=dict, repr=False)
    _response_cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _clients_lock: Any = field(default_factory=Lock, repr=False)
    _response_cache_lock: Any = field(default_factory=Lock, repr=False)
    _state_lock: Any = field(default_factory=Lock, repr=False)
    snapshot_store: CollectionSnapshotStore | None = None
    # Manifesto explícito do operador. Ausente/vazio sempre testa a AWS; nunca
    # é preenchido automaticamente a partir do primeiro AccessDenied.
    denied_iam_actions: frozenset[str] = frozenset()

    def client(self, service: str) -> Any:
        """Um cliente por serviço, criado uma vez.

        Cada `session.client(...)` lê e monta o modelo do serviço. A fonte do
        Athena sozinha pedia sete clientes, e `glue` era reconstruído em cinco
        fontes diferentes — trabalho repetido antes da primeira chamada AWS.
        """
        client = self._clients.get(service)
        if client is not None:
            return client
        with self._clients_lock:
            if service not in self._clients:
                client = InstrumentedClient(
                    make_client(self.session, service),
                    service,
                    self.telemetry,
                    self._response_cache,
                    cache_lock=self._response_cache_lock,
                    denied_iam_actions=self.denied_iam_actions,
                )
                if service == "cloudwatch":
                    client._metric_batch_coordinator = MetricBatchCoordinator(
                        client,
                        telemetry=self.telemetry,
                    )
                self._clients[service] = client
            return self._clients[service]


@dataclass(frozen=True)
class Source:
    """Uma fonte de coleta e tudo que a saúde precisa saber sobre ela."""

    name: str
    collect: Callable[[CollectionContext], Any]
    impact: str
    next_action: str
    required: bool = False
    required_capabilities: frozenset[str] = frozenset()
    # Família de retenção. Declarada em `_SOURCE_FAMILIES` e aplicada por
    # `replace`, para não repetir metadado em 40 blocos.
    family: str = ""
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
    # Dependências do scheduler. Fontes sem dependência podem sobrepor latência;
    # uma fonte só é liberada depois que todas as anteriores declaradas foram
    # aplicadas ao Account.
    depends_on: frozenset[str] = frozenset()
    # Serviços boto3 usados pela fonte. O scheduler adquire limites em ordem
    # estável para impedir deadlock quando uma fonte usa vários clientes.
    services: frozenset[str] = frozenset()
    # Conservador por default: fonte nova roda em série até provar que o collect
    # não escreve em objetos compartilhados. O apply sempre ocorre no agregador.
    parallel_safe: bool = False
    # Cache é opt-in por fonte e exige serialização, escopo e versão explícitos.
    snapshot_policy: SnapshotPolicy | None = None


@dataclass
class SourceResult:
    """Resultado isolado de uma fonte, ainda não aplicado ao inventário."""

    source: Source
    context: CollectionContext
    value: Any
    entries: list[CollectionHealth]
    should_apply: bool = True
    cache_age_seconds: int | None = None


def run(source: Source, ctx: CollectionContext, recorder: CollectionRecorder) -> None:
    """Compatibilidade serial: executa e aplica uma fonte imediatamente."""
    try:
        result = execute(source, ctx)
    except RequiredCollectionError as exc:
        recorder.entries.extend(getattr(exc, "collection_entries", []))
        raise
    recorder.entries.extend(apply_result(result))


def execute(source: Source, ctx: CollectionContext) -> SourceResult:
    """Executa rede/coletor sem publicar resultado no ``Account``.

    A janela que a fonte recebe é recortada no teto de retenção da família dela.
    O recorte vale sempre, não só no bootstrap: é invariante — nunca pedir a uma
    fonte mais dias do que a AWS retém — e com a janela padrão de 30 dias ele não
    morde nenhuma família, então o caminho de sempre não muda.
    """
    local = replace(_scoped(ctx, source), gaps=[], iam_gaps={}, pending_health=[])
    recorder = CollectionRecorder()
    if not local.scope_policy.allows(*source.required_capabilities):
        recorder.not_applicable(
            source.name,
            reason=(
                f"fora do perfil {local.scope_policy.profile}: requer "
                f"{', '.join(sorted(source.required_capabilities))}"
            ),
        )
        return SourceResult(source, local, source.default(), recorder.entries, False)
    if (
        local.max_scan_cost_usd is not None
        and not source.required
        and local.telemetry.estimate(local.config.pricing) >= local.max_scan_cost_usd
    ):
        recorder.unavailable(
            source.name,
            category="budget_exceeded",
            impact=(
                f"fonte opcional interrompida no orçamento de "
                f"US$ {local.max_scan_cost_usd:.4f}"
            ),
            next_action="aumentar --max-scan-cost somente após revisar a telemetria",
            affects_status=False,
        )
        return SourceResult(source, local, source.default(), recorder.entries, False)
    if source.enabled is not None and not source.enabled(local):
        recorder.unavailable(
            source.name,
            category=source.disabled_category,
            impact=source.disabled_impact or source.impact,
            next_action=source.disabled_next_action or source.next_action,
            affects_status=source.disabled_affects_status(local),
        )
        return SourceResult(source, local, source.default(), recorder.entries, False)

    if source.snapshot_policy is not None and local.snapshot_store is not None:
        policy = source.snapshot_policy
        hit = local.snapshot_store.load(
            account_id=local.account.account_id,
            region=local.account.region,
            source=source.name,
            scope=policy.scope(local),
            policy=policy,
        )
        local.telemetry.record_snapshot(hit=hit is not None)
        if hit is not None:
            value = recorder.capture(
                source.name,
                lambda: hit.value,
                source.default(),
                required=source.required,
                count=source.count,
                expected=source.expected(local) if source.expected else None,
                data_through=source.data_through,
                impact=source.impact,
                next_action=source.next_action,
            )
            return SourceResult(
                source,
                local,
                value,
                recorder.entries,
                cache_age_seconds=hit.age_seconds,
            )

    try:
        value = recorder.capture(
            source.name,
            lambda: source.collect(local),
            source.default(),
            required=source.required,
            count=source.count,
            expected=source.expected(local) if source.expected else None,
            data_through=source.data_through,
            impact=source.impact,
            next_action=source.next_action,
        )
    except RequiredCollectionError as exc:
        exc.collection_entries = recorder.entries
        raise
    return SourceResult(source, local, value, recorder.entries)


def apply_result(result: SourceResult) -> list[CollectionHealth]:
    """Publica um ``SourceResult`` no agregador, nunca dentro do worker."""
    source = result.source
    ctx = result.context
    if not result.should_apply:
        return result.entries
    value = result.value
    if source.into:
        setattr(ctx.account, source.into, value)
    if source.apply is not None:
        source.apply(ctx, value)
    entry = result.entries[-1]
    if source.after is not None:
        source.after(ctx, value, entry)
    # Com teto por família, "que janela esta fonte mediu" deixa de ser dedutível
    # da janela da conta e passa a ser dado.
    entry.window_days = ctx.window.days
    _record_gaps(ctx, entry)
    if result.cache_age_seconds is not None:
        entry.result_origin = "cached"
        entry.cache_age_seconds = result.cache_age_seconds
    elif (
        source.snapshot_policy is not None
        and ctx.snapshot_store is not None
        and entry.status == "ok"
    ):
        policy = source.snapshot_policy
        ctx.snapshot_store.save(
            account_id=ctx.account.account_id,
            region=ctx.account.region,
            source=source.name,
            scope=policy.scope(ctx),
            policy=policy,
            value=value,
        )
    if ctx.pending_health:
        result.entries.extend(ctx.pending_health)
        ctx.pending_health.clear()
    return result.entries


def _scoped(ctx: CollectionContext, source: Source) -> CollectionContext:
    """`ctx` com a janela da família, compartilhando o resto por referência.

    `replace` monta um objeto novo, mas `account`, `flags`, `gaps`,
    `pending_health` e o cache de clientes continuam sendo os mesmos objetos — o
    que uma fonte escreve segue visível para a seguinte, e nenhum cliente é
    remontado.
    """
    window = ctx.window.capped(retention_ceiling(source.family))
    if window is ctx.window:
        return ctx
    return replace(ctx, window=window)


_SOURCE_CAPABILITIES: dict[str, frozenset[str]] = {
    "Cost Explorer": frozenset({"billing"}),
    "Cost Matrix": frozenset({"billing"}),
    "Glue Jobs": frozenset({"glue_jobs"}),
    "Glue Scripts": frozenset({"glue_jobs"}),
    "Spark Event Logs": frozenset({"glue_jobs", "s3_evidence"}),
    "Glue Catalog": frozenset({"glue_catalog"}),
    "Glue Crawlers": frozenset({"glue_crawlers"}),
    "Glue Triggers": frozenset({"glue_jobs"}),
    "Glue DataBrew": frozenset({"glue_databrew"}),
    "CloudWatch Glue CPU": frozenset({"glue_jobs"}),
    "CloudWatch Glue Observability": frozenset({"glue_jobs"}),
    "Glue Interactive Sessions": frozenset({"glue_interactive_sessions"}),
    "Glue Cost Explorer": frozenset({"glue_jobs", "billing"}),
    "Athena Queries": frozenset({"athena"}),
    "Athena Provisioned Capacity": frozenset({"athena"}),
    "Amazon S3": frozenset({"s3_evidence"}),
    "S3 Config": frozenset({"s3_evidence"}),
    "S3 Prefixes": frozenset({"s3_evidence"}),
    "S3 Access Evidence": frozenset({"s3_evidence"}),
    "S3 Multipart Uploads": frozenset({"s3_evidence"}),
    "S3 Cost Explorer": frozenset({"s3_evidence", "billing"}),
    "SageMaker Studio": frozenset({"sagemaker"}),
    "SageMaker Spaces": frozenset({"sagemaker"}),
    "SageMaker Domains": frozenset({"sagemaker"}),
    "SageMaker Endpoints": frozenset({"sagemaker"}),
    "SageMaker Notebooks": frozenset({"sagemaker"}),
    "SageMaker Jobs": frozenset({"sagemaker"}),
    "SageMaker Feature Store": frozenset({"sagemaker"}),
    "SageMaker Pipelines": frozenset({"sagemaker"}),
    "SageMaker Model Monitor": frozenset({"sagemaker"}),
    "SageMaker Inference Recommender": frozenset({"sagemaker"}),
    "SageMaker Cost Explorer": frozenset({"sagemaker", "billing"}),
    "SageMaker Savings Plans": frozenset({"sagemaker", "billing"}),
    "Amazon Redshift": frozenset({"redshift"}),
    "Redshift Cost Explorer": frozenset({"redshift", "billing"}),
    "Step Functions": frozenset({"stepfunctions"}),
    "EventBridge Schedules": frozenset({"stepfunctions"}),
    "Table Touches": frozenset({"athena"}),
    "DataWarm Mapping": frozenset({"glue_catalog"}),
    "CloudTrail Ownership": frozenset({"ownership"}),
}


#: A qual retenção cada fonte está sujeita. Por família, e não por fonte, porque
#: uma fonte de custo tem de medir a **mesma** janela do inventário com que ela
#: reconcilia: se `Glue Jobs` medisse 90 dias e `Glue Cost Explorer` 45, o delta
#: de reconciliação explodiria e o rateio deixaria de fechar sem que a causa
#: aparecesse em lugar nenhum. O Cost Explorer nunca é o limitante (retém 12+
#: meses), então o teto é sempre o da telemetria da família.
_SOURCE_FAMILIES: dict[str, str] = {
    "Cost Explorer": "billing",
    "Cost Matrix": "billing",
    "Glue Jobs": "glue",
    "Glue Scripts": "glue",
    "Spark Event Logs": "glue",
    "Glue Catalog": "glue",
    "Glue Crawlers": "glue",
    "Glue Triggers": "glue",
    "Glue DataBrew": "glue",
    "CloudWatch Glue CPU": "glue",
    "CloudWatch Glue Observability": "glue",
    "Glue Interactive Sessions": "glue",
    "Glue Cost Explorer": "glue",
    "Athena Queries": "athena",
    "Athena Provisioned Capacity": "athena",
    "Amazon S3": "s3",
    "S3 Config": "s3",
    "S3 Prefixes": "s3",
    "S3 Access Evidence": "s3",
    "S3 Multipart Uploads": "s3",
    "S3 Cost Explorer": "s3",
    "SageMaker Studio": "sagemaker",
    "SageMaker Spaces": "sagemaker",
    "SageMaker Domains": "sagemaker",
    "SageMaker Endpoints": "sagemaker",
    "SageMaker Notebooks": "sagemaker",
    "SageMaker Jobs": "sagemaker",
    "SageMaker Feature Store": "sagemaker",
    "SageMaker Pipelines": "sagemaker",
    "SageMaker Model Monitor": "sagemaker",
    "SageMaker Inference Recommender": "sagemaker",
    "SageMaker Cost Explorer": "sagemaker",
    "SageMaker Savings Plans": "sagemaker",
    "Amazon Redshift": "redshift",
    "Redshift Cost Explorer": "redshift",
    "Step Functions": "stepfunctions",
    "EventBridge Schedules": "stepfunctions",
    # Toques saem do histórico de queries do Athena: mesma retenção de 45 dias.
    "Table Touches": "athena",
    "DataWarm Mapping": "glue",
    "CloudTrail Ownership": "ownership",
}


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
    structured = list(ctx.iam_gaps.values())
    structured.extend(gaps_from_text(anotados, declared_actions=entry.next_action))
    if structured:
        merged: dict[tuple[str, str], IamGap] = {}
        for gap in structured:
            key = (gap.service, gap.operation)
            current = merged.get(key)
            if current is None:
                merged[key] = gap
                continue
            current.affected_resources += gap.affected_resources
            for example in gap.examples:
                if example not in current.examples and len(current.examples) < 3:
                    current.examples.append(example)
        entry.iam_gaps = sorted(
            merged.values(), key=lambda gap: (gap.service, gap.operation)
        )
        actions = sorted({gap.iam_action for gap in entry.iam_gaps})
        entry.next_action = "validar permissões read-only: " + ", ".join(actions)


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


def _s3_config_snapshot_policy() -> SnapshotPolicy:
    return SnapshotPolicy(
        ttl_seconds=15 * 60,
        collector_version="s3-config-v1",
        serialize=lambda values: [asdict(value) for value in values],
        deserialize=lambda values: [S3BucketConfig(**value) for value in values],
        scope=lambda ctx: {
            "buckets": sorted(s3.bucket_names(_s3_scope(ctx))),
            "scope_profile": ctx.scope_policy.profile,
        },
    )


def _glue_triggers_snapshot_policy() -> SnapshotPolicy:
    """Definição de trigger muda pouco e não contém histórico ou métrica."""
    return SnapshotPolicy(
        ttl_seconds=5 * 60,
        collector_version="glue-triggers-v1",
        serialize=lambda values: [asdict(value) for value in values],
        deserialize=lambda values: [GlueTrigger(**value) for value in values],
        scope=lambda ctx: {"scope_profile": ctx.scope_policy.profile},
    )


def _eventbridge_schedules_snapshot_policy() -> SnapshotPolicy:
    """Regras e targets são configuração; não carregam histórico de execução."""
    return SnapshotPolicy(
        ttl_seconds=5 * 60,
        collector_version="eventbridge-schedules-v1",
        serialize=lambda values: [asdict(value) for value in values],
        deserialize=lambda values: [Schedule(**value) for value in values],
        scope=lambda ctx: {"scope_profile": ctx.scope_policy.profile},
    )


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


def _collect_stepfunctions(ctx: CollectionContext) -> list:
    account_metrics: dict[str, int] = {}
    machines = stepfunctions.collect_state_machines(
        ctx.client("stepfunctions"),
        cloudwatch_client=ctx.client("cloudwatch"),
        account_metrics=account_metrics,
        window=ctx.window,
        gaps=ctx.gaps,
    )
    ctx.account.stepfunctions_map_backlog = account_metrics.get("map_backlog", 0)
    ctx.account.stepfunctions_open_executions = account_metrics.get(
        "open_executions", 0
    )
    ctx.account.stepfunctions_service_integration_failures = account_metrics.get(
        "service_integration_failures", 0
    )
    ctx.account.stepfunctions_service_integration_timeouts = account_metrics.get(
        "service_integration_timeouts", 0
    )
    return machines


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
    cached = ctx.flags.get("s3_prefixes")
    if cached is not None:
        return cached
    with ctx._state_lock:
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
        ordem = list(conhecidos)
        coletados: list = []
        if ctx.s3_inventory:
            inventory, conhecidos = s3_inventory.collect_prefixes(
                client,
                known=conhecidos,
                window=ctx.window,
                stale_after_days=stale_after,
                gaps=ctx.gaps,
            )
            coletados.extend(inventory)
        if conhecidos:
            coletados.extend(
                s3.collect_prefixes(
                    client,
                    known=conhecidos,
                    window=ctx.window,
                    stale_after_days=stale_after,
                    max_pages=None if ctx.s3_full_listing else MAX_LIST_PAGES,
                    # O teto limita custo e cobertura; concorrência apenas
                    # sobrepõe latência de prefixos independentes.
                    workers=S3_LISTING_WORKERS,
                )
            )
        por_alvo = {
            (item.bucket, item.prefix, item.kind, item.source_asset): item
            for item in coletados
        }
        for location, target_kind, source in ordem:
            parsed = parse_location(location)
            if parsed is not None:
                item = por_alvo.get((parsed[0], parsed[1], target_kind, source))
                if item is not None:
                    out.append(item)
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
        name="Cost Matrix",
        collect=lambda ctx: billing_matrix.collect(ctx.client("ce"), window=ctx.window),
        default=lambda: None,
        count=lambda matrix: 1 if matrix else 0,
        apply=lambda ctx, matrix: ctx.flags.__setitem__("billing_matrix", matrix),
        impact=(
            "rateios de Glue, S3, SageMaker e Redshift voltam a consultar "
            "Cost Explorer separadamente"
        ),
        next_action="validar ce:GetCostAndUsage agrupado por SERVICE e USAGE_TYPE",
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
        snapshot_policy=_glue_triggers_snapshot_policy(),
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
                matrix=ctx.flags.get("billing_matrix"),
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
            configured_workgroups=ctx.athena_history_workgroups,
            configured_workgroup_roles=ctx.athena_workgroup_roles,
        ),
        default=lambda: None,
        apply=_apply_athena_analysis,
        after=_publish_athena_dependencies,
        count=lambda analysis: len(analysis.queries) if analysis else 0,
        impact="linhagem de leitura e oportunidades Athena ficam incompletas",
        next_action="validar permissões read-only do Athena",
    ),
    Source(
        name="Athena Provisioned Capacity",
        collect=lambda ctx: athena_capacity.collect_capacity_reservations(
            ctx.client("athena"),
            ctx.client("cloudwatch"),
            ctx.client("ce"),
            window=ctx.window,
            gaps=ctx.gaps,
        ),
        into="athena_capacity_reservations",
        count=len,
        impact="reservas de capacidade e sua utilização não são avaliadas",
        next_action=(
            "validar athena:List/GetCapacityReservation, "
            "GetCapacityAssignmentConfiguration e CloudWatch read-only"
        ),
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
            iam_gaps=ctx.iam_gaps,
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
            "s3:GetAnalyticsConfiguration, s3:GetIntelligentTieringConfiguration, "
            "s3:GetBucketMetadataTableConfiguration e "
            "s3:ListStorageLensConfigurations"
        ),
        after=_flag_buckets_without_access_evidence,
        snapshot_policy=_s3_config_snapshot_policy(),
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
                matrix=ctx.flags.get("billing_matrix"),
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
            matrix=ctx.flags.get("billing_matrix"),
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
                matrix=ctx.flags.get("billing_matrix"),
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
        collect=_collect_stepfunctions,
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
        snapshot_policy=_eventbridge_schedules_snapshot_policy(),
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

# Dependências reais do inventário. A ausência de uma chave é proibida abaixo:
# fonte nova precisa declarar se é raiz ou de quem depende, em vez de herdar a
# posição da tupla como uma dependência implícita.
_SOURCE_DEPENDENCIES: dict[str, frozenset[str]] = {
    "Cost Explorer": frozenset(),
    "Cost Matrix": frozenset(),
    "Glue Jobs": frozenset(),
    "Glue Scripts": frozenset({"Glue Jobs"}),
    "Spark Event Logs": frozenset({"Glue Jobs"}),
    "Glue Catalog": frozenset(),
    "Glue Crawlers": frozenset(),
    "Glue Triggers": frozenset(),
    "Glue DataBrew": frozenset(),
    "CloudWatch Glue CPU": frozenset({"Glue Jobs"}),
    "CloudWatch Glue Observability": frozenset({"Glue Jobs"}),
    "Glue Interactive Sessions": frozenset(),
    "Glue Cost Explorer": frozenset(
        {
            "Cost Matrix",
            "Glue Jobs",
            "Glue Crawlers",
            "Glue DataBrew",
            "Glue Interactive Sessions",
        }
    ),
    # Ambos podem preencher moeda; a dependência preserva a precedência antiga.
    "Athena Queries": frozenset({"Cost Explorer"}),
    "Athena Provisioned Capacity": frozenset(),
    "Amazon S3": frozenset({"Glue Jobs", "Glue Catalog", "Athena Queries"}),
    "S3 Config": frozenset({"Amazon S3"}),
    "S3 Prefixes": frozenset({"Amazon S3"}),
    "S3 Access Evidence": frozenset({"S3 Config", "S3 Prefixes"}),
    "S3 Multipart Uploads": frozenset({"Amazon S3"}),
    "S3 Cost Explorer": frozenset({"Cost Matrix", "Amazon S3"}),
    "SageMaker Studio": frozenset(),
    "SageMaker Spaces": frozenset({"SageMaker Studio"}),
    "SageMaker Domains": frozenset({"SageMaker Studio", "SageMaker Spaces"}),
    "SageMaker Endpoints": frozenset(),
    "SageMaker Notebooks": frozenset(),
    "SageMaker Jobs": frozenset(),
    "SageMaker Feature Store": frozenset(),
    "SageMaker Pipelines": frozenset(),
    "SageMaker Model Monitor": frozenset(),
    "SageMaker Inference Recommender": frozenset(),
    "SageMaker Cost Explorer": frozenset(
        {
            "SageMaker Studio",
            "Cost Matrix",
            "SageMaker Spaces",
            "SageMaker Domains",
            "SageMaker Endpoints",
            "SageMaker Jobs",
            "SageMaker Feature Store",
        }
    ),
    "SageMaker Savings Plans": frozenset(),
    "Amazon Redshift": frozenset(),
    "Redshift Cost Explorer": frozenset({"Cost Matrix", "Amazon Redshift"}),
    "Step Functions": frozenset(),
    "EventBridge Schedules": frozenset(),
    "Table Touches": frozenset({"Glue Catalog"}),
    "DataWarm Mapping": frozenset({"Glue Jobs", "Glue Catalog"}),
    "CloudTrail Ownership": frozenset(),
}

_SOURCE_SERVICES: dict[str, frozenset[str]] = {
    "Cost Explorer": frozenset({"ce"}),
    "Cost Matrix": frozenset({"ce"}),
    "Glue Jobs": frozenset({"glue"}),
    "Glue Scripts": frozenset(),
    "Spark Event Logs": frozenset({"s3"}),
    "Glue Catalog": frozenset({"glue"}),
    "Glue Crawlers": frozenset({"glue"}),
    "Glue Triggers": frozenset({"glue"}),
    "Glue DataBrew": frozenset({"databrew"}),
    "CloudWatch Glue CPU": frozenset({"cloudwatch"}),
    "CloudWatch Glue Observability": frozenset({"cloudwatch"}),
    "Glue Interactive Sessions": frozenset({"glue"}),
    "Glue Cost Explorer": frozenset({"ce"}),
    "Athena Queries": frozenset(
        {"athena", "cloudwatch", "cloudtrail", "identitystore", "glue", "s3", "ce"}
    ),
    "Athena Provisioned Capacity": frozenset({"athena", "cloudwatch", "ce"}),
    "Amazon S3": frozenset({"cloudwatch", "s3"}),
    "S3 Config": frozenset({"s3", "s3control"}),
    "S3 Prefixes": frozenset({"s3"}),
    "S3 Access Evidence": frozenset({"s3"}),
    "S3 Multipart Uploads": frozenset({"s3"}),
    "S3 Cost Explorer": frozenset({"ce"}),
    "SageMaker Studio": frozenset({"sagemaker", "cloudwatch"}),
    "SageMaker Spaces": frozenset({"sagemaker"}),
    "SageMaker Domains": frozenset({"sagemaker", "cloudwatch"}),
    "SageMaker Endpoints": frozenset(
        {"sagemaker", "cloudwatch", "application-autoscaling"}
    ),
    "SageMaker Notebooks": frozenset({"sagemaker"}),
    "SageMaker Jobs": frozenset({"sagemaker", "cloudwatch"}),
    "SageMaker Feature Store": frozenset({"sagemaker", "cloudwatch"}),
    "SageMaker Pipelines": frozenset({"sagemaker"}),
    "SageMaker Model Monitor": frozenset({"sagemaker"}),
    "SageMaker Inference Recommender": frozenset({"sagemaker"}),
    "SageMaker Cost Explorer": frozenset({"ce"}),
    "SageMaker Savings Plans": frozenset({"ce"}),
    "Amazon Redshift": frozenset({"redshift", "cloudwatch", "redshift-serverless"}),
    "Redshift Cost Explorer": frozenset({"ce"}),
    "Step Functions": frozenset({"stepfunctions", "cloudwatch"}),
    "EventBridge Schedules": frozenset({"events"}),
    "Table Touches": frozenset({"athena"}),
    "DataWarm Mapping": frozenset(),
    "CloudTrail Ownership": frozenset({"cloudtrail"}),
}

# Somente collectors que constroem um valor novo entram aqui. Enriquecimentos
# sobre objetos existentes e rateios que mutam o Account permanecem seriais.
_PARALLEL_SAFE_SOURCES = frozenset(
    {
        "Cost Explorer",
        "Cost Matrix",
        "Glue Jobs",
        "Glue Scripts",
        "Glue Catalog",
        "Glue Crawlers",
        "Glue Triggers",
        "Glue DataBrew",
        "Glue Interactive Sessions",
        "Athena Queries",
        "Athena Provisioned Capacity",
        "Amazon S3",
        "S3 Config",
        "S3 Prefixes",
        "S3 Multipart Uploads",
        "SageMaker Studio",
        "SageMaker Spaces",
        "SageMaker Domains",
        "SageMaker Endpoints",
        "SageMaker Notebooks",
        "SageMaker Jobs",
        "SageMaker Feature Store",
        "SageMaker Pipelines",
        "SageMaker Model Monitor",
        "SageMaker Inference Recommender",
        "SageMaker Savings Plans",
        "Amazon Redshift",
        "EventBridge Schedules",
        "Table Touches",
        "CloudTrail Ownership",
    }
)

# A declaração fica próxima do registro sem repetir metadados em 40 blocos.
# `replace` mantém Source imutável e um teste garante que toda fonte foi classificada.
if {source.name for source in SOURCES} != set(_SOURCE_CAPABILITIES):
    missing = {source.name for source in SOURCES} - set(_SOURCE_CAPABILITIES)
    extra = set(_SOURCE_CAPABILITIES) - {source.name for source in SOURCES}
    raise RuntimeError(f"capabilities de fontes inconsistentes: missing={missing}, extra={extra}")

# Fonte sem família herdaria a profundidade cheia em silêncio, que é o modo de
# falha que o teto existe para evitar. Falha na importação, como as capabilities.
if {source.name for source in SOURCES} != set(_SOURCE_FAMILIES):
    missing = {source.name for source in SOURCES} - set(_SOURCE_FAMILIES)
    extra = set(_SOURCE_FAMILIES) - {source.name for source in SOURCES}
    raise RuntimeError(f"famílias de fontes inconsistentes: missing={missing}, extra={extra}")

for registry_name, registry in (
    ("dependências", _SOURCE_DEPENDENCIES),
    ("serviços", _SOURCE_SERVICES),
):
    if {source.name for source in SOURCES} != set(registry):
        missing = {source.name for source in SOURCES} - set(registry)
        extra = set(registry) - {source.name for source in SOURCES}
        raise RuntimeError(
            f"{registry_name} de fontes inconsistentes: missing={missing}, extra={extra}"
        )
if not _PARALLEL_SAFE_SOURCES <= {source.name for source in SOURCES}:
    raise RuntimeError(
        "fontes paralelas desconhecidas: "
        f"{sorted(_PARALLEL_SAFE_SOURCES - {source.name for source in SOURCES})}"
    )

SOURCES = tuple(
    replace(
        source,
        required_capabilities=_SOURCE_CAPABILITIES[source.name],
        family=_SOURCE_FAMILIES[source.name],
        depends_on=_SOURCE_DEPENDENCIES[source.name],
        services=_SOURCE_SERVICES[source.name],
        parallel_safe=source.name in _PARALLEL_SAFE_SOURCES,
    )
    for source in SOURCES
)
