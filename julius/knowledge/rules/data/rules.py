"""Detector de dados: base gerada por processo recorrente sem toques (sem uso)."""

from __future__ import annotations

from julius.collection.models import Account, Table
from julius.config import Config
from julius.knowledge.rules.data import estimation as data_est
from julius.findings.opportunity import Opportunity
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.recommendation import Recommendation

_DOC = "https://docs.aws.amazon.com/glue/latest/dg/tables-described.html"

_GB = 1024**3


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for table in account.tables:
        if table.temporary:
            continue
        writer = account.job_by_name(table.written_by)
        if writer is None or writer.runs_per_month < th.recurring_runs_min:
            continue  # sem job recorrente escritor não há compute a recuperar

        if table.touches_90d <= th.unused_touches_max:
            out.append(_unused(account, table, writer, config, scan_id))
        elif table.touches_90d <= th.low_touches_max and table.consuming_communities <= 1:
            out.append(_low_use(account, table, writer, config, scan_id))
    return out


def _low_use(account, table: Table, writer, config: Config, scan_id: str) -> Opportunity:
    est = data_est.low_use_saving(table, writer, config)
    return build(
        Finding(
            asset_type="table",
            asset_name=table.name,
            rule_id="DATA-LOW-USE-SINGLE-CONSUMER",
            rule_version="1.0.0",
            title="Base recorrente pouco usada por um único consumidor",
            why=(
            f"'{table.name}' teve só {table.touches_90d} toques em 90 dias por "
            f"{table.consuming_communities} comunidade — o job '{writer.name}' roda "
            f"{writer.runs_per_month}×/mês para pouco uso."
        ),
            source_process=writer.name,
        ),
        Recommendation(
            difficulty=3,
            action="Revisar: descomissionar/consolidar ou formalizar como produto",
            how_to_apply=(
            f"Falar com o único consumidor de '{table.name}': se o uso não justifica a recorrência, "
            f"reduzir a frequência do '{writer.name}' ou consolidar; se for estratégico, formalizar o produto."
        ),
            how_to_validate="Após ajustar a frequência/consolidar, medir toques e DPU-h por mês.",
            risks=["o único consumidor pode ser crítico", "reduzir frequência pode defasar o dado"],
            docs=[_DOC],
            risk=0.6,
        ),
        Evidence(
            items=[
            f"{table.touches_90d} toques em 90 dias",
            f"{table.consuming_communities} comunidade / {table.consuming_accounts} conta(s) consumidora(s)",
            f"escrita por '{writer.name}' ({writer.runs_per_month}×/mês)",
        ],
            sources=["Toques (Glue Catalog)", "linhagem (JOB_WRITES_TABLE)"],
            observed_runs=writer.observed_runs,
            coverage_days=table.coverage_days or writer.coverage_days,
            has_optional_metrics=table.coverage_days >= config.thresholds.min_coverage_days,
            owner_tag=table.owner_tag or writer.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _unused(account, table: Table, writer, config: Config, scan_id: str) -> Opportunity:
    est = data_est.unused_output_saving(table, writer, config)
    size_gb = table.storage_bytes / _GB if table.storage_bytes else 0
    return build(
        Finding(
            asset_type="table",
            asset_name=table.name,
            rule_id="DATA-UNUSED-OUTPUT",
            rule_version="1.0.0",
            title="Base recorrente sem uso (ninguém toca)",
            why=(
            f"O job '{writer.name}' gera '{table.name}' {writer.runs_per_month}×/mês, mas a tabela "
            f"teve {table.touches_90d} toques em 90 dias — ninguém consome."
        ),
            source_process=writer.name,
        ),
        Recommendation(
            difficulty=3,
            action="Pausar/descomissionar o processo que gera a base sem uso",
            how_to_apply=(
            f"Confirmar com o dono que '{table.name}' não é usada, pausar o job '{writer.name}' "
            "e arquivar a base; se houver consumidor não mapeado, formalizar o produto."
        ),
            how_to_validate="Após pausar, confirmar ausência de toques e a queda de DPU-h/custo do job.",
            risks=["consumidor não mapeado", "base pode ser exigência regulatória/retenção"],
            docs=[_DOC],
            risk=0.7,
        ),
        Evidence(
            items=[
            f"{table.touches_90d} toques em 90 dias",
            f"escrita por '{writer.name}' ({writer.runs_per_month}×/mês)",
            f"{table.consuming_communities} comunidades / {table.consuming_accounts} contas consumidoras",
            f"~{size_gb:.0f} GB armazenados" if size_gb else "tamanho não informado",
        ],
            sources=["Toques (Glue Catalog)", "linhagem (JOB_WRITES_TABLE)", "Glue GetJobRuns"],
            observed_runs=writer.observed_runs,
            coverage_days=table.coverage_days or writer.coverage_days,
            has_optional_metrics=table.coverage_days >= config.thresholds.min_coverage_days,
            owner_tag=table.owner_tag or writer.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )
