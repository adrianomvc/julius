"""Detectores de Athena: (4) leitura excessiva, (5) sem filtro de partição."""

from __future__ import annotations

from julius.config import Config
from julius.estimation import athena as athena_est
from julius.inventory.model import Account, AthenaQuery
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC_PARTITION = "https://docs.aws.amazon.com/athena/latest/ug/partitions.html"
_DOC_REUSE = "https://docs.aws.amazon.com/athena/latest/ug/reusing-query-results.html"

_GB = 1024**3


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for q in account.athena_queries:
        # Regra 5 — sem filtro de partição (tem precedência sobre leitura excessiva).
        if q.table_is_partitioned and not q.has_partition_filter:
            out.append(_no_partition(account, q, config, scan_id))
            continue
        # Regra 4 — leitura excessiva (SELECT * / scan alto).
        if q.data_scanned_bytes >= th.athena_high_scan_bytes or q.selects_star:
            out.append(_excessive_scan(account, q, config, scan_id))
            continue
        # Regra 6 — query recorrente sem result reuse (query "saudável", mas repetida).
        if not q.result_reuse_enabled and q.executions_per_month >= 8:
            out.append(_result_reuse(account, q, config, scan_id))
    return out


def _result_reuse(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    est = athena_est.result_reuse_saving(q, config)
    return build(
        account=account.account_id, asset_type="athena_query", asset_name=q.query_id,
        rule_id="ATHENA-RESULT-REUSE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding="Query recorrente sem query result reuse",
        why=f"Query roda {q.executions_per_month}×/mês no workgroup sem result reuse — reprocessa resultados idênticos.",
        recommended_action="Habilitar query result reuse no workgroup",
        how_to_apply="Ativar result reuse no workgroup (ou por query) com janela de reuso adequada.",
        how_to_validate="Comparar bytes escaneados e cache hits no workgroup.",
        evidence=["workgroup sem result reuse", f"{q.executions_per_month} execuções/mês", "dados de origem estáveis"],
        risks=["resultado defasado se a janela de reuso for longa demais"],
        doc_links=[_DOC_REUSE], data_sources=["Athena workgroup history"],
        observed_runs=q.observed_runs, coverage_days=q.coverage_days,
        has_optional_metrics=q.observed_runs >= config.thresholds.min_runs,
        owner_tag=q.owner_tag, config=config, scan_id=scan_id,
    )


def _no_partition(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    est = athena_est.partition_pruning_saving(q, config)
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-NO-PARTITION-FILTER",
        rule_version="1.0.0",
        difficulty=2,
        estimation=est,
        finding="Query sem filtro de partição varre a tabela inteira",
        why=(
            f"Query recorrente varre {q.data_scanned_bytes / (1024**4):.2f} TB/execução "
            f"sem predicado de partição; {q.executions_per_month} execuções/mês."
        ),
        recommended_action="Adicionar filtro de partição (WHERE dt=…)",
        how_to_apply="Adicionar filtro de partição e projetar só as colunas usadas.",
        how_to_validate="Comparar DataScannedInBytes antes × depois na mesma janela.",
        evidence=[
            f"DataScannedInBytes {q.data_scanned_bytes / (1024**3):.0f} GB por execução",
            "sem filtro de partição (dt)",
            f"{q.executions_per_month} execuções/mês no histórico",
        ],
        risks=["resultado incompleto se a partição errada for filtrada"],
        doc_links=[_DOC_PARTITION],
        data_sources=["Athena get_query_execution", "workgroup history"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=True,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _excessive_scan(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    est = athena_est.projection_saving(q, config)
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-EXCESSIVE-SCAN",
        rule_version="1.0.0",
        difficulty=2,
        estimation=est,
        finding="Query com leitura excessiva (SELECT * / sem result reuse)",
        why=(
            f"Varre {q.data_scanned_bytes / _GB:.0f} GB/execução; "
            f"{'SELECT * ' if q.selects_star else ''}"
            f"{'sem result reuse no workgroup' if not q.result_reuse_enabled else ''}."
        ),
        recommended_action="Projetar colunas e habilitar result reuse",
        how_to_apply="Selecionar só as colunas necessárias e ativar query result reuse no workgroup.",
        how_to_validate="Comparar bytes escaneados e cache hits no workgroup.",
        evidence=[
            f"{q.data_scanned_bytes / _GB:.0f} GB escaneados por execução",
            "SELECT * detectado" if q.selects_star else "leitura ampla de colunas",
            "workgroup sem result reuse" if not q.result_reuse_enabled else "result reuse ativo",
        ],
        risks=["quebra de compatibilidade se colunas removidas forem usadas"],
        doc_links=[_DOC_REUSE],
        data_sources=["Athena workgroup history"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=q.observed_runs >= config.thresholds.min_runs,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )
