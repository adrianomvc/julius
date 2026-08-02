"""Campo que decide cifra precisa ter quem o escreva — e quem o leia.

`redundant_read_bytes_window` e `incremental_source_evidence` ficaram declarados
no modelo e lidos pela regra de bookmark, sem nenhum coletor os preenchendo. O
efeito não falhava em teste nem em produção: a regra simplesmente nunca produzia
número, e um achado permanentemente sem economia é indistinguível de um em que a
economia é zero de verdade.

A metade simétrica existe e custa dinheiro de verdade: campo que **alguém
escreve e ninguém lê**. Ele não some em silêncio como o anterior — ele consome
chamada de API, cota e tempo de coleta em toda execução, para não influenciar
nenhuma regra, nenhuma cifra e nenhuma linha de relatório. Uma auditoria em
agosto de 2026 encontrou 129 deles.

A técnica é a mesma de `scripts/check_read_only_aws.py`, que já varre o fonte
atrás de um padrão: aqui o padrão é "alguém atribui este campo" e, na segunda
metade, "alguém a jusante o menciona".

**O que esta rede não pega**, dito para ninguém confiar nela além do que ela
cobre. A verificação é por *nome*, não por modelo: um campo homônimo escrito em
outro dataclass conta como escritor e produz um falso verde — e o mesmo vale
para leitura. E há um caminho de escrita legítimo que só é visível porque os
coletores declaram o alvo — o `_enrich` de CloudWatch preenche por `setattr` com
nome vindo de tabela, então o nome aparece como chave de dicionário e não como
atribuição.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
COLETA = RAIZ / "julius" / "collection"
MODELOS = COLETA / "models"

#: Onde um campo precisa aparecer para ter deixado de ser custo puro de coleta.
#: Uso interno da própria coleta não conta: `_apply_workload_history` agrupar
#: jobs por `workload_fingerprint` é meio, não fim — nada sai daquilo.
CONSUMO = ("knowledge", "reporting", "scoring", "graph", "findings", "analysis",
           "state", "governance")

#: Campos cuja ausência não deixa a regra falhar — deixa a cifra sumir. Cada
#: entrada é `(campo, para que serve)`, e a segunda parte vira a mensagem de erro
#: quando ninguém escreve o campo.
CAMPOS_QUE_DECIDEM_CIFRA = [
    ("redundant_read_bytes_window", "bytes relidos sem bookmark"),
    ("incremental_source_evidence", "fonte incremental, condição do bookmark"),
    ("bytes_read_window", "leitura da janela, base do reprocessamento"),
    ("date_partitioned", "partição temporal do prefixo de origem"),
    ("get_requests_window", "GETs por prefixo, base da compactação"),
    ("allocated_cost", "custo rateado do Cost Explorer"),
    ("modeled_cost", "custo modelado por tarifa"),
    ("dpu_seconds_window", "DPU-segundo faturado do Glue"),
    ("failure_categories", "categoria de falha, base do timeout"),
    ("last_run_at", "última execução, distingue job parado de job sem dado"),
]


def _fontes_da_coleta() -> list[tuple[Path, str]]:
    return [
        (caminho, caminho.read_text(encoding="utf-8"))
        for caminho in COLETA.rglob("*.py")
        if "__pycache__" not in caminho.parts
    ]


@pytest.mark.parametrize(
    ("campo", "proposito"),
    CAMPOS_QUE_DECIDEM_CIFRA,
    ids=[campo for campo, _ in CAMPOS_QUE_DECIDEM_CIFRA],
)
def test_a_field_that_decides_a_figure_has_someone_writing_it(campo, proposito):
    """Campo sem escritor produz cifra ausente em silêncio, não erro."""
    # Quatro formas de escrever, todas em uso no repositório: kwarg na
    # construção do dataclass, atribuição posterior, `setattr` com nome literal,
    # e declaração como chave de tabela — que é como o `_enrich` de CloudWatch
    # diz qual campo cada métrica preenche antes de aplicar por `setattr`.
    padrao = re.compile(
        rf"(?:\b{campo}\s*=|\.\s*{campo}\s*=|setattr\([^,]+,\s*[\"']{campo}[\"']"
        rf"|[\"']{campo}[\"']\s*:)"
    )
    escritores = sorted(
        caminho.relative_to(RAIZ).as_posix()
        for caminho, fonte in _fontes_da_coleta()
        # O próprio modelo declara o default; declarar não é escrever.
        if "models" not in caminho.parts and padrao.search(fonte)
    )

    assert escritores, (
        f"nenhum coletor escreve `{campo}` ({proposito}). "
        "A regra que o consome nunca vai produzir cifra, e nada mais vai avisar."
    )


def test_the_guard_would_catch_a_field_nobody_writes():
    """A rede só serve se pegar o caso que ela existe para pegar."""
    padrao = re.compile(r"\bcampo_que_ninguem_escreve\s*=")
    escritores = [
        caminho
        for caminho, fonte in _fontes_da_coleta()
        if padrao.search(fonte)
    ]

    assert escritores == []


#: A dívida herdada, nomeada. Campo coletado que não chega a regra, cifra nem
#: relatório — a lista existe para que ela encolha, nunca para acomodar mais um.
#: Três categorias moram aqui, e o teste não sabe distinguir:
#:
#: - **intermediário legítimo**: `date_partitioned` alimenta
#:   `collection/redundant_reads.py`, `workload_fingerprint` agrupa jobs em
#:   `_apply_workload_history`. O consumidor existe e é interno à coleta;
#: - **evidência a ligar**: `bytes_by_size`, `bytes_by_age` e `duration_p95_ms`
#:   são dinheiro medido esperando regra;
#: - **custo puro**: o resto — chamada de API paga em toda coleta para nada.
SEM_CONSUMIDOR_CONHECIDO = frozenset(
    {
        "access_log_target_bucket",
        "access_log_target_prefix",
        "access_logging_enabled",
        "allocated_buckets",
        "allocated_dpus_p95",
        "allocation_version",
        "api_billed_bytes",
        "api_scanned_bytes",
        "app_count",
        "avg_duration_seconds",
        "base_rpu",
        "bytes_by_age",
        "bytes_by_size",
        "bytes_written_window",
        "cloudwatch_bytes",
        "completed_at",
        "completed_on",
        "connection_names",
        "consumed_dpu_hours",
        "cost_basis",
        "created_on",
        "current_copies",
        "current_on_demand_spend",
        "database_name",
        "date_partitioned",
        "definition_available",
        "desired_copies",
        "disk_p95",
        "dpu_seconds",
        "dpu_seconds_window",
        "duration_p95_ms",
        "efs_cost_metric",
        "efs_cost_quality",
        "efs_read_io_bytes",
        "efs_storage_cost",
        "efs_write_io_bytes",
        "encrypted",
        "ended_at",
        "endpoint_config_name",
        "engine_p95_ms",
        "estimated_monthly_commitment",
        "event_source",
        "exact_fingerprint",
        "execution_history_available",
        "execution_hours_window",
        "execution_source",
        "execution_time_sec",
        "expression",
        "head_requests_window",
        "home_efs_file_system_id",
        "identity_confidence",
        "identity_source",
        "identity_type",
        "in_financial_window",
        "intelligent_tiering_ids",
        "inventory_data_through",
        "job_mode",
        "job_run_queuing_enabled",
        "job_type",
        "keep_alive_seconds",
        "last_activity_at",
        "last_crawl_started_at",
        "last_crawl_status",
        "last_error",
        "last_execution_at",
        "last_execution_time",
        "last_invocation_at",
        "last_modified_at",
        "last_read_source",
        "last_runtime_sec",
        "lifecycle_config_name",
        "lifecycle_rules",
        "list_requests",
        "max_capacity",
        "max_copies",
        "max_execution_sec",
        "max_invocations",
        "median_runtime_sec",
        "metadata_table_enabled",
        "metrics_enabled",
        "min_copies",
        "model_name",
        "monitoring_type",
        "nonzero_object_count",
        "object_count_by_age",
        "offline_store",
        "oldest_submission",
        "owner_user_profile",
        "p50_ms",
        "p95_ms",
        "partition_keys",
        "partition_projection_enabled",
        "period_kind",
        "planning_p95_ms",
        "platform_identifier",
        "price_performance_target",
        "queries_in_window",
        "redshift_cost_coverage",
        "resource_arn",
        "sagemaker_cost_coverage",
        "scaling_policy_count",
        "select_requests_window",
        "server_errors",
        "serverless_memory_mb",
        "sharing_type",
        "space_count",
        "space_name",
        "spark_event_log_objects_scanned",
        "started_at",
        "storage_class_analysis_ids",
        "storage_lens_enabled",
        "storage_type",
        "succeeded",
        "trigger_type",
        "user_profile_name",
        "variant_name",
        "workflow_name",
        "workgroup_output_locations",
        "workload_fingerprint",
    }
)


def _campos_declarados() -> dict[str, set[str]]:
    """Cada campo anotado nos dataclasses de modelo, e em quais classes."""
    campos: dict[str, set[str]] = {}
    for caminho in sorted(MODELOS.glob("*.py")):
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for classe in ast.walk(arvore):
            if not isinstance(classe, ast.ClassDef):
                continue
            for item in classe.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    campos.setdefault(item.target.id, set()).add(classe.name)
    return campos


def _fontes_a_jusante() -> list[str]:
    """Tudo que fica **depois** da coleta, incluindo o desenho do relatório."""
    fontes = [
        caminho.read_text(encoding="utf-8")
        for area in CONSUMO
        for caminho in (RAIZ / "julius" / area).rglob("*.py")
        if "__pycache__" not in caminho.parts
    ]
    fontes += [
        caminho.read_text(encoding="utf-8", errors="ignore")
        for pasta in ("templates", "assets")
        for caminho in (RAIZ / "julius" / "reporting" / pasta).rglob("*")
        if caminho.is_file()
    ]
    return fontes


def _sem_consumidor() -> set[str]:
    jusante = _fontes_a_jusante()
    return {
        campo
        for campo in _campos_declarados()
        if not any(re.search(rf"\b{re.escape(campo)}\b", fonte) for fonte in jusante)
    }


def test_a_new_model_field_reaches_something_downstream():
    """Campo novo sem leitor é chamada de API paga para produzir nada.

    A lista de exceção nomeia a dívida que já existia. Um campo fora dela e sem
    consumidor é dívida nova, e a hora de decidir se ela vale a pena é agora —
    não na próxima auditoria.
    """
    novos = _sem_consumidor() - SEM_CONSUMIDOR_CONHECIDO

    assert not novos, (
        f"campos coletados que não chegam a regra, cifra ou relatório: {sorted(novos)}. "
        "Ligue o campo a um consumidor, ou remova-o junto da chamada que o preenche."
    )


def test_the_debt_list_only_shrinks():
    """Campo que saiu da dívida sai da lista — senão ela vira decoração.

    Sem esta metade, `SEM_CONSUMIDOR_CONHECIDO` só cresceria: ninguém lembra de
    apagar uma linha de um `frozenset` ao escrever uma regra nova.

    Há duas formas de sair da dívida, e a mensagem separa as duas porque a ação
    de quem lê o erro é a mesma mas a leitura não: um campo ligado a uma regra
    é dívida paga, um campo removido é dívida cancelada.
    """
    declarados = set(_campos_declarados())
    ligados = (SEM_CONSUMIDOR_CONHECIDO & declarados) - _sem_consumidor()
    removidos = SEM_CONSUMIDOR_CONHECIDO - declarados

    assert not ligados, (
        f"estes campos já têm consumidor a jusante: {sorted(ligados)}. "
        "Remova-os de SEM_CONSUMIDOR_CONHECIDO."
    )
    assert not removidos, (
        f"estes campos não existem mais no modelo: {sorted(removidos)}. "
        "Remova-os de SEM_CONSUMIDOR_CONHECIDO."
    )


def test_the_downstream_guard_would_catch_a_field_nobody_reads():
    """A segunda rede também precisa provar que pega o caso dela."""
    jusante = _fontes_a_jusante()

    assert not any(
        re.search(r"\bcampo_que_ninguem_le\b", fonte) for fonte in jusante
    )
