"""Orquestrador: coleta ao vivo (boto3) → Account (mesmo modelo do dataset exportado)."""

from __future__ import annotations

from datetime import date

import boto3

from julius.aws import (
    athena_collector,
    cloudtrail_collector,
    cloudwatch_collector,
    cost_explorer,
    crawlers_collector,
    databrew_collector,
    datawarm_collector,
    glue_collector,
    glue_triggers_collector,
    schedules_collector,
    sessions_collector,
    spark_event_logs_collector,
    stepfunctions_collector,
    touches_collector,
)
from julius.aws.collection_health import CollectionRecorder, RequiredCollectionError
from julius.inventory.model import Account


def collect_account(
    session: boto3.Session,
    *,
    account_id: str | None = None,
    lookback_days: int = 90,
    touches_table: str = "",
    athena_workgroup: str = "julius",
    athena_output: str | None = None,
    include_cloudtrail: bool = False,
    datawarm_job: str = "",
) -> Account:
    health = CollectionRecorder()
    region = session.region_name or "us-east-1"
    actual_id = health.capture(
        "AWS identity",
        lambda: str(session.client("sts").get_caller_identity()["Account"]),
        "",
        required=True,
        count=lambda value: 1 if value else 0,
        expected=1,
        impact="sem identidade verificada não é seguro atribuir o scan",
        next_action="renovar o login SSO e verificar o Account ID",
    )
    if account_id and account_id != actual_id:
        health.entries[-1].status = "error"
        health.entries[-1].error_category = "identity_mismatch"
        raise RequiredCollectionError("AWS identity", "identity_mismatch")
    ident = account_id or actual_id
    period = date.today().strftime("%b/%Y")

    glue = session.client("glue")
    account = Account(
        account_id=ident,
        region=region,
        period=period,
        lookback_days=lookback_days,
        generated_at=date.today().isoformat(),
    )
    account.services = health.capture(
        "Cost Explorer",
        lambda: cost_explorer.collect_services(
            session.client("ce"), include_forecast=True
        ),
        [],
        count=len,
        data_through=_latest_data_through,
        impact="cobrança MTD, forecast e reconciliação ficam indisponíveis",
        next_action="validar ce:GetCostAndUsage e ce:GetCostForecast",
    )
    if account.services:
        account.currency = account.services[0].currency
    account.glue_jobs = health.capture(
        "Glue Jobs",
        lambda: glue_collector.collect_jobs(glue, lookback_days=lookback_days),
        [],
        required=True,
        count=len,
        data_through=_latest_data_through,
        impact="sem inventário de jobs o scan Glue não é confiável",
        next_action="validar glue:GetJobs e glue:GetJobRuns",
    )
    health.unavailable(
        "Glue Scripts",
        category="separate_stage_required",
        impact="análise estática de código ainda não foi executada",
        next_action="executar julius agent collect-artifacts e reutilizar o manifesto",
        affects_status=any(job.script_location for job in account.glue_jobs),
    )
    event_log_jobs = [
        job for job in account.glue_jobs if job.spark_event_logs_path
    ]
    if event_log_jobs:
        health.capture(
            "Spark Event Logs",
            lambda: _enrich_return(
                lambda: spark_event_logs_collector.enrich_glue_shuffle(
                    session.client("s3"),
                    account.glue_jobs,
                    lookback_days=lookback_days,
                ),
                event_log_jobs,
            ),
            [],
            count=lambda jobs: sum(
                1 for job in jobs if job.spark_event_log_objects_scanned > 0
            ),
            expected=len(event_log_jobs),
            impact="shuffle, spill e evidência de código permanecem investigações",
            next_action="validar s3:GetObject e o --spark-event-logs-path dos jobs",
        )
        if any(
            job.spark_event_log_objects_scanned > 0
            and not job.spark_event_log_evidence_complete
            for job in event_log_jobs
        ):
            entry = health.entries[-1]
            entry.status = "partial"
            entry.error_category = "bounded_or_incomplete"
            entry.impact = "shuffle e spill usam somente evidência parcial"
            entry.next_action = "revisar limites, objetos truncados e prefixo dos event logs"
    else:
        health.unavailable(
            "Spark Event Logs",
            category="not_configured",
            impact="shuffle e spill não podem ser confirmados por logs",
            next_action="configurar --spark-event-logs-path nos jobs relevantes",
            affects_status=bool(account.glue_jobs),
        )
    account.tables = health.capture(
        "Glue Catalog",
        lambda: glue_collector.collect_tables(glue),
        [],
        count=len,
        impact="linhagem e oportunidades de tabelas ficam incompletas",
        next_action="validar glue:GetDatabases e glue:GetTables",
    )
    account.glue_crawlers = health.capture(
        "Glue Crawlers",
        lambda: crawlers_collector.collect_crawlers(glue),
        [],
        count=len,
        data_through=_latest_data_through,
        impact="falhas, recrawl e schedules de crawlers não são avaliados",
        next_action="validar glue:GetCrawlers, glue:GetCrawlerMetrics e glue:ListCrawls",
    )
    account.glue_triggers = health.capture(
        "Glue Triggers",
        lambda: glue_triggers_collector.collect_triggers(glue),
        [],
        count=len,
        impact="frequência e grafo de processos podem ficar incompletos",
        next_action="validar glue:GetTriggers",
    )
    account.databrew_jobs = health.capture(
        "Glue DataBrew",
        lambda: databrew_collector.collect_jobs(session.client("databrew")),
        [],
        count=len,
        data_through=_latest_data_through,
        impact="custos e falhas do DataBrew não são avaliados",
        next_action="validar permissões read-only do DataBrew",
    )
    # Enriquece com CPU (CloudWatch) → destrava as regras de capacidade.
    health.capture(
        "CloudWatch Glue CPU",
        lambda: _enrich_return(
            lambda: cloudwatch_collector.enrich_glue_cpu(
                session.client("cloudwatch"),
                account.glue_jobs,
                lookback_days=lookback_days,
            ),
            account.glue_jobs,
        ),
        [],
        count=lambda jobs: sum(job.avg_cpu_load is not None for job in jobs),
        expected=len(account.glue_jobs),
        impact="recomendações de capacidade permanecem bloqueadas",
        next_action="habilitar métricas Glue e validar cloudwatch:GetMetricStatistics",
    )
    health.capture(
        "CloudWatch Glue Observability",
        lambda: _enrich_return(
            lambda: cloudwatch_collector.enrich_glue_observability(
                session.client("cloudwatch"),
                account.glue_jobs,
                lookback_days=lookback_days,
            ),
            account.glue_jobs,
        ),
        [],
        count=lambda jobs: sum(
            job.avg_worker_utilization is not None
            or job.max_memory_used_pct is not None
            or job.max_disk_used_pct is not None
            or job.max_task_skew is not None
            for job in jobs
        ),
        expected=len(account.glue_jobs),
        impact="memória, disco, skew e executor gap ficam incompletos",
        next_action="habilitar Glue Observability e validar métricas no CloudWatch",
    )
    account.interactive_sessions = health.capture(
        "Glue Interactive Sessions",
        lambda: sessions_collector.collect_sessions(glue),
        [],
        count=len,
        data_through=_latest_data_through,
        impact="ociosidade e capacidade de sessões não são avaliadas",
        next_action="validar glue:ListSessions e glue:ListStatements",
    )
    # Athena: coleta descritiva dos últimos 30 dias completos (UTC), read-only.
    athena_analysis = health.capture(
        "Athena Queries",
        lambda: athena_collector.collect_analysis(
            session.client("athena"),
            cloudwatch_client=session.client("cloudwatch"),
            cloudtrail_client=session.client("cloudtrail"),
            identitystore_client=session.client("identitystore"),
            glue_client=glue,
            s3_client=session.client("s3"),
            ce_client=session.client("ce"),
            lookback_days=30,
        ),
        None,
        count=lambda analysis: len(analysis.queries) if analysis else 0,
        impact="linhagem de leitura e oportunidades Athena ficam incompletas",
        next_action="validar permissões read-only do Athena",
    )
    if athena_analysis is not None:
        account.athena_queries = athena_analysis.queries
        account.athena_actor_usage = athena_analysis.actors
        account.athena_coverage = athena_analysis.coverage
        if athena_analysis.coverage.cost_metric:
            account.currency = athena_analysis.coverage.currency or account.currency
    account.state_machines = health.capture(
        "Step Functions",
        lambda: stepfunctions_collector.collect_state_machines(
            session.client("stepfunctions"), lookback_days=lookback_days
        ),
        [],
        count=len,
        impact="grafo de processos e frequência podem ficar incompletos",
        next_action="validar states:ListStateMachines e histórico read-only",
    )
    account.schedules = health.capture(
        "EventBridge Schedules",
        lambda: schedules_collector.collect_schedules(session.client("events")),
        [],
        count=len,
        impact="frequência esperada dos processos pode ficar incompleta",
        next_action="validar events:ListRules e events:ListTargetsByRule",
    )
    # Toques (opcional): enriquece as tabelas com nº de acessos/consumidores.
    # Degradação graciosa: sem tabela de toques configurada, nada acontece.
    if touches_table:
        stats = health.capture(
            "Table Touches",
            lambda: touches_collector.collect_touches(
                session.client("athena"),
                touches_table=touches_table,
                workgroup=athena_workgroup,
                output_location=athena_output,
                lookback_days=lookback_days,
            ),
            {},
            count=len,
            impact="uso e consumidores das tabelas ficam incompletos",
            next_action="validar tabela, workgroup e saída Athena configurados",
        )
        _merge_touches(account, stats)
    else:
        health.unavailable(
            "Table Touches",
            category="not_configured",
            impact="uso e consumidores das tabelas não são avaliados",
            next_action="informar --touches-table para habilitar a fonte",
            affects_status=False,
        )
    if datawarm_job:
        health.capture(
            "DataWarm Mapping",
            lambda: _enrich_return(
                lambda: datawarm_collector.mark_publications(account, datawarm_job),
                account.tables,
            ),
            [],
            count=lambda tables: sum(table.datawarm_published for table in tables),
            impact="publicações DataWarm podem não ser reconhecidas",
            next_action="validar o identificador informado em --datawarm-job",
        )
    else:
        health.unavailable(
            "DataWarm Mapping",
            category="not_enabled",
            impact="publicações DataWarm não são classificadas",
            next_action="usar --datawarm-job quando essa governança for necessária",
            affects_status=False,
        )
    if include_cloudtrail:
        account.actor_events = health.capture(
            "CloudTrail Ownership",
            lambda: cloudtrail_collector.collect_actor_events(
                session.client("cloudtrail"), lookback_days=lookback_days
            ),
            [],
            count=len,
            impact="responsáveis inferidos podem permanecer desconhecidos",
            next_action="validar cloudtrail:LookupEvents",
        )
    else:
        health.unavailable(
            "CloudTrail Ownership",
            category="not_enabled",
            impact="responsáveis dependem de tags e fallbacks existentes",
            next_action="usar --cloudtrail quando a atribuição de autoria for necessária",
            affects_status=False,
        )
    account.collection_health = health.entries
    return account


def _merge_touches(account: Account, stats: dict) -> None:
    for table in account.tables:
        s = stats.get(table.name)
        if s is not None:
            table.touches_90d = s.touches
            table.consuming_accounts = s.accounts
            table.consuming_communities = s.communities
            table.used_by_accounts = list(s.account_ids)
            table.primary_community = s.primary_community


def _enrich_return(fn, value):
    fn()
    return value


def _latest_data_through(items) -> str:
    values = [
        str(getattr(item, "data_through", "") or getattr(item, "cost_data_through", ""))
        for item in items
    ]
    return max((value for value in values if value), default="")
