"""Coleta ao vivo (boto3) → Account, o mesmo modelo do dataset exportado.

O orquestrador não sabe quais fontes existem: ele monta a janela, verifica a
identidade e percorre `SOURCES`. Fonte nova é uma linha de dado em
`collection/sources.py`, não uma inserção no meio desta função.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3

from julius.collection.checkpoints import (
    CheckpointStore,
    DomainCheckpointWriter,
    resume_ready_domains,
)
from julius.collection.collectors.account_name import collect_account_name
from julius.collection.collectors.last_read import apply_last_read
from julius.collection.health import CollectionRecorder, RequiredCollectionError
from julius.collection.models import Account
from julius.collection.policy import ScopePolicy, policy_for_profile
from julius.collection.redundant_reads import apply_redundant_reads
from julius.collection.scheduler import run_sources
from julius.collection.scope import CatalogScope, normalize
from julius.collection.session import make_client
from julius.collection.settings import ANALYSIS_WINDOW_DAYS
from julius.collection.snapshot import CollectionSnapshotStore
from julius.collection.sources import SOURCES, CollectionContext
from julius.collection.window import AnalysisWindow, BillingMonth


def collect_account(
    session: boto3.Session,
    *,
    config: Any,
    account_id: str | None = None,
    lookback_days: int = ANALYSIS_WINDOW_DAYS,
    touches_table: str = "",
    athena_workgroup: str = "julius",
    athena_history_workgroups: tuple[str, ...] = (),
    athena_workgroup_roles: dict[str, str] | None = None,
    athena_output: str | None = None,
    include_cloudtrail: bool = False,
    datawarm_job: str = "",
    catalog_scope: CatalogScope | None = None,
    s3_full_listing: bool = False,
    s3_inventory: bool = False,
    sagemaker_full_metrics: bool = False,
    scope_policy: ScopePolicy | None = None,
    max_scan_cost_usd: float | None = None,
    max_parallel_pages: int = 8,
    max_memory_mb: int | None = None,
    now: datetime | None = None,
    window: AnalysisWindow | None = None,
    cadence: str = "weekly",
    bootstrap: bool = False,
    collection_execution: str = "parallel",
    snapshot_store: CollectionSnapshotStore | None = None,
    scan_id: str = "",
    run_store: CheckpointStore | None = None,
    checkpoint_dir: str | Path | None = None,
    enqueue_domain_ai: bool = True,
    denied_iam_actions: frozenset[str] = frozenset(),
    resume_checkpoints: bool = False,
) -> Account:
    """Coleta uma conta. `config` chega de cima e não tem default aqui.

    A camada de coleta não conhece a classe de configuração nem as tabelas de
    domínio que ela carrega: recebe o objeto, lê atributos nomeados e repassa.
    É o que mantém a seta apontando só para baixo.
    """
    health = CollectionRecorder()
    # Duas janelas, construídas uma vez, ambas em UTC. Nenhum coletor volta a
    # decidir sozinho qual período está olhando.
    window = window or AnalysisWindow.trailing(days=lookback_days, now=now)
    billing = BillingMonth.current(now=now)

    ident = _verified_identity(session, health, account_id)
    catalog_scope = _named_catalog_scope(session, health, catalog_scope)
    policy = scope_policy or policy_for_profile(None)
    account = Account(
        account_id=ident,
        scope_profile=policy.profile,
        s3_mode=policy.s3_mode,
        region=session.region_name or "us-east-1",
        period=window.label,
        cadence=cadence,
        financial_period=(
            window.start_date.strftime("%Y-%m") if cadence == "monthly" else ""
        ),
        lookback_days=window.days,
        generated_at=window.end.date().isoformat(),
        scan_id=scan_id,
        window_start=window.start_date.isoformat(),
        window_end=window.data_through.isoformat(),
        window_days=window.days,
        bootstrap=bootstrap,
    )
    context = CollectionContext(
        session=session,
        window=window,
        billing=billing,
        account=account,
        config=config,
        scope_policy=policy,
        telemetry=account.run_telemetry,
        max_scan_cost_usd=max_scan_cost_usd,
        max_parallel_pages=max_parallel_pages,
        max_memory_mb=max_memory_mb,
        touches_table=touches_table,
        athena_workgroup=athena_workgroup,
        athena_history_workgroups=athena_history_workgroups,
        athena_workgroup_roles=athena_workgroup_roles or {},
        athena_output=athena_output,
        include_cloudtrail=include_cloudtrail,
        datawarm_job=datawarm_job,
        catalog_scope=catalog_scope or CatalogScope(),
        s3_full_listing=s3_full_listing,
        s3_inventory=s3_inventory,
        sagemaker_full_metrics=sagemaker_full_metrics,
        glue_usage_markers=config.glue_cost.usage_type_markers,
        allocatable_glue_buckets=config.glue_cost.allocatable_buckets,
        glue_cost_version=config.glue_cost.version,
        redshift_usage_markers=config.redshift_cost.usage_type_markers,
        redshift_compute_buckets=config.redshift_cost.compute_buckets,
        redshift_cost_version=config.redshift_cost.version,
        sagemaker_usage_markers=config.sagemaker_cost.usage_type_markers,
        allocatable_sagemaker_buckets=config.sagemaker_cost.allocatable_buckets,
        sagemaker_cost_version=config.sagemaker_cost.version,
        snapshot_store=snapshot_store,
        denied_iam_actions=denied_iam_actions,
    )

    checkpoint_writer = None
    completed_sources: frozenset[str] = frozenset()
    if run_store is not None:
        if not scan_id or checkpoint_dir is None:
            raise ValueError(
                "scan_id e checkpoint_dir são obrigatórios quando run_store é usado"
            )
        run_store.create_run(ident, scan_id)
        supersede = getattr(run_store, "supersede_older_context", None)
        if not resume_checkpoints and callable(supersede):
            superseded_runs, superseded_jobs = supersede(ident, scan_id)
            account.run_telemetry.superseded_runs = superseded_runs
            account.run_telemetry.superseded_ai_jobs = superseded_jobs
        if run_store.run_status(ident, scan_id) == "created":
            run_store.transition(ident, scan_id, "collecting")
        fingerprint = _collection_fingerprint(
            context,
            collection_execution=collection_execution,
            include_cloudtrail=include_cloudtrail,
        )
        if resume_checkpoints:
            resumed, resumed_health = resume_ready_domains(
                run_store,
                account,
                scan_id,
                collection_fingerprint=fingerprint,
            )
            completed_sources = frozenset(resumed)
            health.entries.extend(resumed_health)
        checkpoint_writer = DomainCheckpointWriter(
            run_store,
            checkpoint_dir,
            account,
            scan_id,
            enqueue_ai=enqueue_domain_ai,
            collection_fingerprint=fingerprint,
        )

    try:
        run_sources(
            SOURCES,
            context,
            health,
            execution=collection_execution,
            on_source_applied=(
                checkpoint_writer.source_completed if checkpoint_writer else None
            ),
            completed_sources=completed_sources,
        )
    except RequiredCollectionError:
        if checkpoint_writer is not None:
            checkpoint_writer.wait(raise_errors=False)
        if run_store is not None and run_store.run_status(ident, scan_id) == "collecting":
            run_store.transition(ident, scan_id, "collection_partial")
        raise
    except BaseException:
        if checkpoint_writer is not None:
            checkpoint_writer.wait(raise_errors=False)
        raise
    if checkpoint_writer is not None:
        checkpoint_writer.wait()
    if run_store is not None:
        queue_stats = getattr(run_store, "queue_stats", None)
        if callable(queue_stats):
            queue = queue_stats()
            account.run_telemetry.ai_queue_depth = int(queue.pending + queue.running)
            account.run_telemetry.ai_queue_oldest_wait_ms = int(
                queue.oldest_pending_ms
            )
            account.run_telemetry.ai_queue_rejected = int(queue.rejected)

    # Derivação pura, depois de tudo coletado: a última leitura de uma tabela
    # sai do histórico de queries do Athena, e é o que liga um prefixo S3 a uma
    # data de **leitura** em vez de só a data da última escrita. Não faz chamada
    # AWS, então não é fonte — mas o alcance dela entra na saúde, porque
    # recomendar classe de armazenamento depende inteiramente dessa cobertura.
    _record_read_evidence(account, health)
    # Segunda derivação pura: cruza o que o job leu (CloudWatch) com o tamanho da
    # fonte (listagem S3) para medir reprocessamento. Sem chamada AWS, e por isso
    # fora de `SOURCES`.
    apply_redundant_reads(account)

    account.collection_health = health.entries
    account.run_telemetry.estimate(config.pricing)
    if run_store is not None and run_store.run_status(ident, scan_id) in {
        "collecting",
        "collection_partial",
    }:
        run_store.transition(ident, scan_id, "deterministic_ready")
    return account


def _collection_fingerprint(
    context: CollectionContext,
    *,
    collection_execution: str,
    include_cloudtrail: bool,
) -> str:
    payload = {
        "window": {
            "start": context.window.start.isoformat(),
            "end": context.window.end.isoformat(),
            "days": context.window.days,
        },
        "scope_policy": context.scope_policy,
        "catalog_scope": context.catalog_scope,
        "config": context.config,
        "touches_table": context.touches_table,
        "athena_workgroup": context.athena_workgroup,
        "athena_history_workgroups": context.athena_history_workgroups,
        "athena_workgroup_roles": context.athena_workgroup_roles,
        "athena_output": context.athena_output,
        "include_cloudtrail": include_cloudtrail,
        "datawarm_job": context.datawarm_job,
        "s3_full_listing": context.s3_full_listing,
        "s3_inventory": context.s3_inventory,
        "sagemaker_full_metrics": context.sagemaker_full_metrics,
        "max_parallel_pages": context.max_parallel_pages,
        "max_memory_mb": context.max_memory_mb,
        "collection_execution": collection_execution,
        "denied_iam_actions": context.denied_iam_actions,
    }
    encoded = json.dumps(
        _canonical(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _record_read_evidence(account: Account, health: CollectionRecorder) -> None:
    total = len(account.tables)
    if not total:
        return
    medidas = apply_last_read(account)
    if medidas == total:
        return
    health.unavailable(
        "Última leitura de tabelas",
        category="bounded_or_incomplete" if medidas else "no_data",
        impact=(
            f"{total - medidas} de {total} tabela(s) sem data de última leitura: "
            "para elas só se conhece a última escrita, que não diz se o dado é usado"
        ),
        next_action=(
            "configurar --touches-table, ampliar a janela do histórico Athena, "
            "ou habilitar server access logging nos buckets"
        ),
        affects_status=False,
    )


def _named_catalog_scope(
    session: boto3.Session,
    health: CollectionRecorder,
    scope: CatalogScope | None,
) -> CatalogScope:
    """Completa o escopo com o nome que a própria conta informa.

    Roda aqui, e não no CLI, por três razões: a identidade já foi confirmada, a
    chamada passa pelo recorder e aparece na saúde, e a telemetria a conta como
    qualquer outra. Uma chamada AWS solta no CLI não teria nenhuma das três.

    Consulta **sempre** que o nome importa, mesmo quando o cadastro já resolveu —
    é o que permite comparar. Sem comparação, cadastro desatualizado envelhece
    calado, e o sintoma é um recorte de catálogo que não casa com nada.

    Substitui somente quando a origem é `profile`: esse não é um nome de conta, é
    o apelido que alguém deu ao perfil. Quem declarou explicitamente ou cadastrou
    à mão teve uma razão, e uma API não a sobrescreve.
    """
    scope = scope or CatalogScope()
    if scope.databases:
        # A lista explícita substitui a regra de nome inteira: o nome não é usado,
        # e pedir dado de contato para descartá-lo seria gratuito.
        return scope

    da_conta = health.capture(
        "AWS account name",
        lambda: collect_account_name(session),
        "",
        count=lambda value: 1 if value else 0,
        expected=1,
        impact=(
            "sem o nome da conta o recorte do Glue Catalog usa o apelido do perfil "
            "SSO, que não tem relação com a conta"
        ),
        next_action=(
            "conceder account:GetContactInformation, ou cadastrar a conta em "
            "~/.julius-accounts.json, ou informar --account-name"
        ),
    )
    # `capture` assume que fonte não obrigatória degrada o scan, e esta não pode:
    # não saber o nome não estraga nenhuma medição, só estreita o recorte do
    # catálogo. Quem reporta essa consequência é `Glue Catalog Scope`, que também
    # não degrada — as duas precisam dizer a mesma coisa sobre a mesma coisa.
    health.entries[-1].affects_status = False
    if not da_conta:
        return scope
    if scope.name_source == "profile" or not scope.account_name:
        return replace(scope, account_name=da_conta, name_source="aws")
    if normalize(scope.account_name) != normalize(da_conta):
        health.entries[-1].status = "partial"
        health.entries[-1].error_category = "name_mismatch"
        health.entries[-1].impact = (
            f"a conta se chama {da_conta!r} na AWS e {scope.account_name!r} na "
            f"origem {scope.name_source!r}, que prevalece"
        )
        health.entries[-1].next_action = (
            "conferir ~/.julius-accounts.json, ou --account-name, contra o nome da conta"
        )
    return scope


def _verified_identity(
    session: boto3.Session, health: CollectionRecorder, account_id: str | None
) -> str:
    """Sem identidade confirmada não é seguro atribuir o scan a uma conta."""
    actual = health.capture(
        "AWS identity",
        lambda: str(make_client(session, "sts").get_caller_identity()["Account"]),
        "",
        required=True,
        count=lambda value: 1 if value else 0,
        expected=1,
        impact="sem identidade verificada não é seguro atribuir o scan",
        next_action="renovar o login SSO e verificar o Account ID",
    )
    if account_id and account_id != actual:
        health.entries[-1].status = "error"
        health.entries[-1].error_category = "identity_mismatch"
        raise RequiredCollectionError("AWS identity", "identity_mismatch")
    return account_id or actual
