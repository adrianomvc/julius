"""Oportunidades determinísticas derivadas de código de AWS Glue."""

from __future__ import annotations

from dataclasses import dataclass

from julius.code_analysis import GlueCodeArtifact, scan_glue_script
from julius.collection.models import Account, GlueJob
from julius.config import Config
from julius.estimation.glue import code_pattern_saving, python_shell_migration_saving
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC_TUNING = "https://docs.aws.amazon.com/glue/latest/dg/tuning-aws-glue-for-apache-spark.html"
_DOC_PUSHDOWN = "https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-pushdown.html"
_DOC_SHUFFLE = "https://docs.aws.amazon.com/prescriptive-guidance/latest/tuning-aws-glue-for-apache-spark/optimize-shuffles.html"
_DOC_PYTHON_SHELL = "https://docs.aws.amazon.com/glue/latest/dg/add-job-python.html"
_DOC_PYTHON_MIGRATION = "https://docs.aws.amazon.com/glue/latest/dg/pyshell-migration.html"
_DOC_JOB = "https://docs.aws.amazon.com/glue/latest/webapi/API_Job.html"
_DOC_BOOKMARK = "https://docs.aws.amazon.com/glue/latest/dg/monitor-continuations.html"


@dataclass(frozen=True)
class RuleSpec:
    finding: str
    why: str
    action: str
    apply: str
    validate: str
    fraction: float
    difficulty: int
    risk: float
    doc: str


_RULES: dict[str, RuleSpec] = {
    "GLUE-CODE-PUSHDOWN": RuleSpec(
        "Leitura catalogada sem predicate pushdown",
        "Filtrar somente depois da leitura pode trazer partições desnecessárias ao cluster Glue.",
        "Aplicar push_down_predicate ou catalogPartitionPredicate",
        "Mover filtros de partição para a leitura e preservar a mesma semântica do dataset.",
        "Comparar bytes lidos, DPUSeconds, duração e contagem de saída no mesmo volume.",
        0.20, 2, 0.45, _DOC_PUSHDOWN,
    ),
    "GLUE-CODE-S3-FULL-SCAN": RuleSpec(
        "Leitura direta de prefixo S3 filtrada somente depois",
        "O cluster pode listar e ler arquivos que seriam descartados pelo filtro Spark.",
        "Particionar a origem e podar caminhos/partições antes da leitura",
        "Usar tabela catalogada com pushdown ou construir somente os caminhos necessários.",
        "Comparar bytes/arquivos lidos, duração, DPUSeconds e resultado.",
        0.20, 3, 0.55, _DOC_PUSHDOWN,
    ),
    "GLUE-CODE-JDBC-SINGLE-READER": RuleSpec(
        "Leitura JDBC sem paralelismo explícito",
        "Uma única conexão de leitura pode deixar workers ociosos e prolongar o Glue.",
        "Avaliar leitura JDBC particionada",
        "Escolher coluna uniforme, limites e número de partições sem sobrecarregar a origem.",
        "Comparar conexões, throughput, carga no banco, duração e DPUSeconds.",
        0.15, 4, 0.70, _DOC_PUSHDOWN,
    ),
    "GLUE-CODE-SINGLE-PARTITION": RuleSpec(
        "Código força processamento em uma única partição",
        "repartition(1)/coalesce(1) elimina paralelismo e pode prolongar a cobrança do job.",
        "Remover a partição única ou separar compactação da transformação",
        "Escolher partições pelo volume de saída e compactar somente quando necessário.",
        "Comparar duração, tasks ativas, tamanho e quantidade dos arquivos de saída.",
        0.15, 2, 0.50, _DOC_TUNING,
    ),
    "GLUE-CODE-DRIVER-MATERIALIZATION": RuleSpec(
        "Código materializa dados distribuídos no driver",
        "collect/toPandas pode concentrar memória, causar falhas e desperdiçar retries cobrados.",
        "Manter processamento distribuído ou limitar explicitamente o conjunto",
        "Substituir a materialização por agregação/filtro distribuído e coleta de resultado pequeno.",
        "Validar memória do driver, duração, falhas e equivalência da saída.",
        0.10, 3, 0.60, _DOC_TUNING,
    ),
    "GLUE-CODE-PYTHON-UDF": RuleSpec(
        "Python UDF pode impedir otimizações do Spark",
        "Funções Python por linha adicionam serialização e podem aumentar a duração do Glue.",
        "Preferir funções Spark SQL nativas",
        "Substituir a UDF quando existir função nativa equivalente; manter teste de paridade.",
        "Executar benchmark A/B e comparar CPU, duração, DPUSeconds e resultado.",
        0.10, 3, 0.55, _DOC_TUNING,
    ),
    "GLUE-CODE-REPEATED-ACTIONS": RuleSpec(
        "Múltiplas ações Spark podem recomputar o plano",
        "Ações repetidas podem reler e recalcular o mesmo conjunto de dados.",
        "Eliminar ações de diagnóstico ou materializar somente o resultado reutilizado",
        "Remover count/show/collect redundantes e persistir apenas quando houver reutilização comprovada.",
        "Comparar número de jobs/stages, bytes lidos, duração e DPUSeconds.",
        0.10, 2, 0.45, _DOC_TUNING,
    ),
    "GLUE-CODE-CACHE-LIFECYCLE": RuleSpec(
        "Cache Spark sem encerramento observável",
        "Persistência desnecessária pressiona memória e pode provocar spill ou workers maiores.",
        "Remover cache sem reutilização ou executar unpersist após o último uso",
        "Confirmar reutilização do DataFrame e delimitar o ciclo de vida do cache.",
        "Validar memória, spill, duração e recomputações antes/depois.",
        0.05, 2, 0.45, _DOC_TUNING,
    ),
    "GLUE-CODE-ITERATIVE-PLAN": RuleSpec(
        "Plano Spark construído iterativamente",
        "withColumn/union dentro de loop pode gerar plano extenso e aumentar planejamento e execução.",
        "Consolidar expressões e uniões",
        "Usar uma projeção única ou unionByName sobre uma coleção balanceada.",
        "Comparar tamanho do plano, stages, duração e DPUSeconds.",
        0.10, 3, 0.50, _DOC_TUNING,
    ),
    "GLUE-CODE-ROW-EXTERNAL-IO": RuleSpec(
        "I/O externo executado por registro",
        "Chamadas AWS, HTTP ou banco dentro de UDF/map multiplicam latência e podem causar retries.",
        "Mover I/O para operações em lote ou por partição",
        "Buscar dados de referência antes da transformação ou agrupar chamadas com limites e retry.",
        "Validar quantidade de chamadas, duração, throttling, falhas e resultado.",
        0.15, 4, 0.70, _DOC_TUNING,
    ),
    "GLUE-CODE-FULL-OVERWRITE": RuleSpec(
        "Overwrite sem escopo incremental observável",
        "Regravar o dataset inteiro pode consumir DPU-hora e I/O desnecessários.",
        "Avaliar escrita incremental ou sobrescrita dinâmica de partições",
        "Restringir a escrita às partições afetadas e preservar atomicidade e retenção.",
        "Comparar bytes escritos, arquivos, duração e reconciliação da saída.",
        0.20, 4, 0.65, _DOC_PUSHDOWN,
    ),
    "GLUE-CODE-SMALL-FILES": RuleSpec(
        "Código pode produzir quantidade excessiva de arquivos",
        "Particionamento excessivo e writes em loop aumentam tempo de escrita e custo downstream.",
        "Dimensionar partições de saída e consolidar writes",
        "Calibrar repartition/coalesce pelo volume, sem forçar partição única.",
        "Medir quantidade/tamanho dos arquivos, duração e DPUSeconds.",
        0.10, 3, 0.55, _DOC_TUNING,
    ),
    "GLUE-CODE-SHUFFLE": RuleSpec(
        "Transformações com potencial de shuffle relevante",
        "Joins, agregações e reparticionamentos podem dominar duração, disco e rede.",
        "Revisar chaves, particionamento e estratégia de join",
        "Correlacionar o trecho com Spark UI; aplicar broadcast somente se o lado menor couber no executor.",
        "Comparar shuffle read/write, spill, skew, duração e resultado.",
        0.15, 4, 0.65, _DOC_SHUFFLE,
    ),
    "GLUE-CODE-SHUFFLE-PARTITIONS": RuleSpec(
        "Número fixo extremo de partições de shuffle",
        "Partições demais aumentam overhead e arquivos; poucas reduzem paralelismo e elevam spill.",
        "Calibrar spark.sql.shuffle.partitions com métricas do workload",
        "Preferir AQE quando suportado e ajustar somente após observar tamanho e tasks.",
        "Comparar tasks, skew, shuffle, spill, duração e DPUSeconds.",
        0.10, 3, 0.60, _DOC_SHUFFLE,
    ),
    "GLUE-CODE-SWALLOWED-EXCEPTION": RuleSpec(
        "Código descarta exceções silenciosamente",
        "Falhas parciais podem produzir reprocessamento, retries manuais e cobrança duplicada.",
        "Registrar contexto seguro e falhar ou tratar explicitamente",
        "Definir quais erros são recuperáveis; aplicar retry limitado somente nesses casos.",
        "Injetar falha controlada e confirmar status, logs, retry e consistência da saída.",
        0.05, 3, 0.70, _DOC_TUNING,
    ),
    "GLUE-CODE-BOOKMARK-CONTEXT": RuleSpec(
        "Contexto de transformação necessário para bookmark não foi observado",
        "Contextos ausentes ou instáveis podem impedir rastreamento incremental e gerar reprocessamento.",
        "Revisar transformation_ctx nas fontes e transformações relevantes",
        "Usar identificadores estáveis e validar a semântica do bookmark em ambiente controlado.",
        "Executar duas cargas incrementais e confirmar que dados antigos não são reprocessados.",
        0.10, 3, 0.65, _DOC_BOOKMARK,
    ),
    "GLUE-CODE-BOOKMARK-COMMIT": RuleSpec(
        "Job inicializado sem commit do estado do bookmark",
        "Sem job.commit o estado incremental pode não avançar e os dados podem ser reprocessados.",
        "Garantir job.commit após conclusão bem-sucedida",
        "Adicionar commit no caminho de sucesso sem mascarar falhas anteriores.",
        "Executar duas cargas incrementais e confirmar avanço do bookmark e ausência de reprocessamento.",
        0.15, 2, 0.65, _DOC_BOOKMARK,
    ),
}


def detect(
    account: Account,
    artifacts: list[GlueCodeArtifact],
    config: Config,
    scan_id: str,
) -> list[Opportunity]:
    found: list[Opportunity] = []
    for artifact in artifacts:
        job = account.job_by_name(artifact.asset_name)
        if job is None:
            continue
        for code_finding in scan_glue_script(artifact.content):
            if (
                code_finding.rule_id.startswith("GLUE-CODE-BOOKMARK-")
                and not job.job_bookmark
            ):
                continue
            if code_finding.rule_id == "GLUE-SPARK-TO-PYTHON-SHELL":
                opportunity = _python_shell_candidate(
                    account, job, artifact, code_finding.lines, config, scan_id
                )
            else:
                spec = _RULES.get(code_finding.rule_id)
                opportunity = (
                    _code_opportunity(
                        account, job, artifact, code_finding.lines, spec,
                        code_finding.rule_id, config, scan_id
                    )
                    if spec
                    else None
                )
            if opportunity is not None:
                found.append(opportunity)
    return found


def _code_opportunity(
    account: Account,
    job: GlueJob,
    artifact: GlueCodeArtifact,
    lines: tuple[int, ...],
    spec: RuleSpec,
    rule_id: str,
    config: Config,
    scan_id: str,
) -> Opportunity:
    correlated = _has_runtime_correlation(rule_id, job)
    estimation = code_pattern_saving(
        job,
        config,
        method=rule_id.lower().replace("-", "_") + "_v1",
        potential_fraction=spec.fraction if correlated else 0.0,
        assumption=spec.why,
    )
    opportunity = build(
        account=account.account_id,
        asset_type="glue_job",
        asset_name=job.name,
        rule_id=rule_id,
        rule_version="1.0.0",
        difficulty=spec.difficulty,
        estimation=estimation,
        finding=spec.finding,
        why=spec.why,
        recommended_action=spec.action,
        how_to_apply=spec.apply,
        how_to_validate=spec.validate,
        evidence=[
            f"script sha256={artifact.sha256[:16]}",
            f"linhas={_line_text(lines)}",
            f"artefato {'truncado' if artifact.truncated else 'completo'}",
        ],
        risks=[
            "alteração de código exige teste de regressão e comparação com o mesmo volume",
        ],
        doc_links=[spec.doc],
        data_sources=["Glue ScriptLocation (S3)", "scanner estático Julius"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=correlated and not artifact.truncated,
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id,
        risk=spec.risk,
        blocked=True,
        source_process=job.name,
    )
    opportunity.missing_evidence = _missing_runtime_evidence(rule_id, job, artifact)
    opportunity.next_action = spec.validate
    opportunity.evidence_refs.append(
        {
            "source": artifact.source or "Glue ScriptLocation",
            "sha256": artifact.sha256,
            "lines": list(lines),
        }
    )
    return opportunity


def _python_shell_candidate(
    account: Account,
    job: GlueJob,
    artifact: GlueCodeArtifact,
    lines: tuple[int, ...],
    config: Config,
    scan_id: str,
) -> Opportunity | None:
    if job.command_type != "glueetl" or artifact.truncated or job.job_bookmark:
        return None
    incompatible = {
        "--extra-files",
        "--extra-py-files",
        "--extra-jars",
        "--user-jars-first",
    } & set(job.default_argument_keys)
    if incompatible:
        return None
    estimation = python_shell_migration_saving(job, config)
    opportunity = build(
        account=account.account_id,
        asset_type="glue_job",
        asset_name=job.name,
        rule_id="GLUE-SPARK-TO-PYTHON-SHELL",
        rule_version="1.0.0",
        difficulty=4,
        estimation=estimation,
        finding="Glue Spark ETL executa script sem uso distribuído detectado",
        why=(
            "O job inicializa capacidade Spark, mas o script completo não contém APIs "
            "Spark/Glue distribuídas; Python Shell pode executar integração Python leve."
        ),
        recommended_action="Executar piloto equivalente em Glue Python Shell 0,0625 DPU",
        how_to_apply=(
            "Criar definição de teste separada com Command.Name=pythonshell e Python 3.9; "
            "não alterar o job atual antes da validação."
        ),
        how_to_validate=(
            "Comparar resultado, duração, memória, dependências, logs e custo por execução "
            "em execuções representativas."
        ),
        evidence=[
            f"Command.Name atual={job.command_type}",
            "nenhuma API Spark/Glue distribuída detectada",
            f"script completo sha256={artifact.sha256[:16]}",
            f"capacidade atual={job.configured_dpu:.4g} DPU",
        ],
        risks=[
            "Python Shell não suporta job bookmarks",
            "Python Shell não suporta --extra-files",
            "armazenamento temporário e memória são locais e limitados",
            "AWS orienta avaliar runtimes alternativos para workloads Python Shell",
        ],
        doc_links=[_DOC_PYTHON_SHELL, _DOC_JOB, _DOC_PYTHON_MIGRATION],
        data_sources=[
            "Glue GetJobs",
            "Glue ScriptLocation (S3)",
            "scanner estático Julius",
        ],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=(
            job.avg_worker_utilization is not None
            or job.avg_cpu_load is not None
        ),
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id,
        risk=0.75,
        blocked=True,
        source_process=job.name,
    )
    opportunity.missing_evidence = [
        "benchmark Python Shell com o mesmo volume",
        "pico de memória e uso de /tmp no piloto",
        "compatibilidade das bibliotecas com Python 3.9/wheels",
        "validação funcional da saída",
    ]
    opportunity.next_action = opportunity.how_to_validate
    opportunity.evidence_refs.append(
        {
            "source": artifact.source or "Glue ScriptLocation",
            "sha256": artifact.sha256,
            "lines": list(lines),
        }
    )
    return opportunity


def _has_runtime_correlation(rule_id: str, job: GlueJob) -> bool:
    if rule_id in {"GLUE-CODE-SHUFFLE", "GLUE-CODE-SINGLE-PARTITION"}:
        return bool(
            job.has_spill_evidence
            or (job.shuffle_write_bytes or 0) > 0
            or (job.max_task_skew or 0) > 1.5
        )
    if rule_id in {"GLUE-CODE-DRIVER-MATERIALIZATION", "GLUE-CODE-CACHE-LIFECYCLE"}:
        return job.max_memory_used_pct is not None
    if rule_id == "GLUE-CODE-SMALL-FILES":
        return job.spark_event_log_evidence_complete
    return job.spark_event_log_evidence_complete


def _missing_runtime_evidence(
    rule_id: str, job: GlueJob, artifact: GlueCodeArtifact
) -> list[str]:
    missing = ["benchmark A/B com mesmo input e validação funcional"]
    if artifact.truncated:
        missing.append("script completo")
    if rule_id in {"GLUE-CODE-SHUFFLE", "GLUE-CODE-SINGLE-PARTITION"} and not (
        job.has_spill_evidence or (job.shuffle_write_bytes or 0) > 0
    ):
        missing.append("Spark Event Log/Glue metrics confirmando shuffle ou gargalo")
    if rule_id in {"GLUE-CODE-DRIVER-MATERIALIZATION", "GLUE-CODE-CACHE-LIFECYCLE"} and (
        job.max_memory_used_pct is None
    ):
        missing.append("pico de memória do driver/executores")
    if rule_id in {"GLUE-CODE-PUSHDOWN", "GLUE-CODE-FULL-OVERWRITE"}:
        missing.append("bytes e partições lidos/escritos antes e depois")
    return missing


def _line_text(lines: tuple[int, ...]) -> str:
    return ",".join(str(line) for line in lines[:20]) or "não disponível"
