"""Oportunidades e sinais derivados do código que os jobs SageMaker executam.

A mesma separação que o Glue já faz, e pela mesma razão: um padrão estático só
vira `Opportunity` quando existe métrica de runtime que o corrobore. Sem ela vai
como `Signal` — o mesmo achado, sem número e sem vaga no ranking, para a análise
contextual julgar contra o script inteiro.

O que muda em relação ao Glue é o que corrobora. Lá a evidência é o Spark event
log; aqui é a telemetria da própria instância, e ela já era coletada: `gpu_p95`
diz se a GPU paga foi usada, e a diferença entre `training_seconds` e
`billable_seconds` diz quanto tempo a instância ficou ligada sem treinar.
"""

from __future__ import annotations

from dataclasses import dataclass

from julius.collection.artifacts import CodeArtifact
from julius.collection.models import Account, SageMakerJob
from julius.config import Config, is_gpu_instance
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal
from julius.knowledge.rules.sagemaker import estimation as sm_est
from julius.knowledge.rules.sagemaker.code_scanner import scan_sagemaker_script
from julius.knowledge.signal_potential import potential

_DOC_SPOT = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/model-managed-spot-training.html"
)
_DOC_CHECKPOINT = "https://docs.aws.amazon.com/sagemaker/latest/dg/model-checkpoints.html"
_DOC_DISTRIBUTED = "https://docs.aws.amazon.com/sagemaker/latest/dg/distributed-training.html"
_DOC_INPUT_MODE = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/model-access-training-data.html"
)
_DOC_INSTANCES = "https://docs.aws.amazon.com/sagemaker/latest/dg/notebooks-available-instance-types.html"
_DOC_METRICS = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/training-metrics.html"
)


@dataclass(frozen=True)
class RuleSpec:
    finding: str
    why: str
    action: str
    apply: str
    validate: str
    difficulty: int
    risk: float
    doc: str
    #: O que precisa ser medido para o padrão virar cifra.
    missing: tuple[str, ...]
    #: Fração típica do custo do job que o padrão pode devolver. Não é medição:
    #: serve para ordenar hipóteses entre si, nunca para prometer economia.
    fraction: float


_RULES: dict[str, RuleSpec] = {
    "SM-CODE-CPU-ONLY-ON-GPU": RuleSpec(
        "Script sem uso de GPU em instância com GPU",
        "A instância cobra aceleração que o código não aciona em nenhum ponto detectável.",
        "Executar piloto equivalente em instância sem GPU",
        "Rodar o mesmo script e o mesmo volume numa instância de propósito geral, "
        "sem alterar o job atual antes da validação.",
        "Comparar duração, uso de CPU e memória, resultado do treino e custo por execução.",
        3,
        0.55,
        _DOC_INSTANCES,
        ("benchmark do mesmo volume na instância alvo", "perfil de CPU e memória do treino"),
        0.6,
    ),
    "SM-CODE-SINGLE-DEVICE-MULTI-INSTANCE": RuleSpec(
        "Mais de uma instância sem treino distribuído no código",
        "O cluster é provisionado e cobrado por inteiro; sem API distribuída, "
        "as instâncias extras não recebem trabalho.",
        "Reduzir para uma instância ou adotar treino distribuído",
        "Escolher entre encolher o cluster e distribuir de fato; as duas mudam o "
        "resultado do treino de formas diferentes.",
        "Comparar duração, utilização por instância e convergência do modelo.",
        3,
        0.60,
        _DOC_DISTRIBUTED,
        ("utilização por instância durante a execução",),
        0.5,
    ),
    "SM-CODE-NO-CHECKPOINT": RuleSpec(
        # O sinal aponta o spot porque é ele que o checkpoint destrava: sem
        # retomada, uma interrupção recomeça o treino inteiro e a economia do
        # spot vira prejuízo.
        "Treino sem checkpoint bloqueia o managed spot",
        "Sem checkpoint uma interrupção recomeça do zero, e é por isso que o "
        "spot — a economia mais direta do treino — não pode ser proposto.",
        "Gravar checkpoint em /opt/ml/checkpoints e avaliar managed spot",
        "Salvar estado em intervalo regular e retomar a partir dele; só então "
        "habilitar spot com prazo máximo declarado.",
        "Interromper um treino de teste e confirmar que ele retoma do checkpoint.",
        3,
        0.50,
        _DOC_CHECKPOINT,
        ("tolerância a interrupção e prazo máximo aceitável",),
        0.5,
    ),
    "SM-CODE-FULL-DATASET-LOAD": RuleSpec(
        "Dataset de entrada carregado inteiro antes do treino",
        "A instância fica ligada e cobrando enquanto o dado desce e é lido, "
        "antes de qualquer época começar.",
        "Avaliar FastFile ou Pipe como modo de entrada",
        "Trocar o modo de entrada e ler o dado em fluxo, preservando a mesma "
        "ordem e o mesmo conjunto.",
        "Comparar tempo até a primeira época, duração total e resultado.",
        3,
        0.55,
        _DOC_INPUT_MODE,
        ("tempo de download versus tempo de treino na execução",),
        0.2,
    ),
    "SM-CODE-FIXED-EPOCHS": RuleSpec(
        "Número fixo de épocas sem parada antecipada",
        "Épocas que não melhoram a métrica custam instância-hora cheia.",
        "Adotar parada antecipada por métrica de validação",
        "Definir métrica, paciência e critério de melhora; manter o teto de épocas.",
        "Comparar épocas executadas, métrica final e duração.",
        2,
        0.45,
        _DOC_METRICS,
        ("curva da métrica de validação por época",),
        0.25,
    ),
    "SM-CODE-ROW-EXTERNAL-IO": RuleSpec(
        "I/O externo executado por registro",
        "Chamada AWS, HTTP ou banco dentro do laço multiplica latência pela "
        "cardinalidade do dado e prolonga a instância-hora.",
        "Mover I/O para operações em lote ou para antes do laço",
        "Buscar dados de referência antes da transformação ou agrupar chamadas "
        "com limite e retry.",
        "Validar quantidade de chamadas, duração, throttling e resultado.",
        4,
        0.70,
        _DOC_METRICS,
        ("quantidade de chamadas externas por execução",),
        0.2,
    ),
    "SM-CODE-SWALLOWED-EXCEPTION": RuleSpec(
        "Código descarta exceções silenciosamente",
        "Falha parcial produz reprocessamento e execuções repetidas já cobradas.",
        "Registrar contexto seguro e falhar ou tratar explicitamente",
        "Definir quais erros são recuperáveis e aplicar retry limitado só neles.",
        "Injetar falha controlada e confirmar status, logs e consistência da saída.",
        3,
        0.70,
        _DOC_METRICS,
        ("execuções que terminaram OK sem produzir o modelo esperado",),
        0.1,
    ),
}

#: Padrão em que a própria configuração fecha a conclusão. Mais de uma instância
#: sem nenhuma API distribuída no script não depende de medir nada: as
#: instâncias extras são cobradas e não há código que as use.
_SELF_EVIDENT = frozenset({"SM-CODE-SINGLE-DEVICE-MULTI-INSTANCE"})


def analysable_jobs(account: Account) -> list[SageMakerJob]:
    """Jobs cujo script existe e pode ser lido de fora do contêiner.

    O denominador da cobertura. Algoritmo gerenciado e script embutido na imagem
    não entram: não é lacuna do Julius não analisar um XGBoost da AWS, e contar
    esses jobs como não cobertos faria uma coleta completa parecer furada.
    """
    return [
        job
        for job in account.sagemaker_jobs
        if job.code_location and job.code_kind in {"sourcedir_tar", "s3_object"}
    ]


def coverage_gaps(account: Account, artifacts: list[CodeArtifact]) -> list[str]:
    """Por que cada job ficou sem análise de código — um silêncio explicado.

    Duas causas, e elas pedem ações diferentes de quem lê: o script existia e o
    bundle não o trouxe (permissão, pacote grande demais) ou não havia script
    para trazer. Sem separar as duas, a segunda faz a primeira desaparecer.
    """
    lidos = {
        artifact.asset_name
        for artifact in artifacts
        if artifact.kind in {"", "sagemaker_script"}
    }
    faltando = [job.name for job in analysable_jobs(account) if job.name not in lidos]
    motivos = sorted(
        {
            job.code_unavailable_reason
            for job in account.sagemaker_jobs
            if job.code_unavailable_reason
        }
    )
    gaps = []
    if faltando:
        gaps.append(
            f"{len(faltando)} job(s) com script declarado e não lido: "
            + ", ".join(sorted(faltando)[:5])
        )
    gaps += [f"sem script para analisar — {motivo}" for motivo in motivos]
    return gaps


def detect(
    account: Account,
    artifacts: list[CodeArtifact],
    config: Config,
    scan_id: str,
) -> tuple[list[Opportunity], list[Signal]]:
    """Separa o que o script prova do que ele apenas sugere."""
    por_nome = {job.name: job for job in analysable_jobs(account)}
    found: list[Opportunity] = []
    signals: list[Signal] = []
    for artifact in artifacts:
        if artifact.kind and artifact.kind != "sagemaker_script":
            continue
        job = por_nome.get(artifact.asset_name)
        if job is None:
            continue
        achados = scan_sagemaker_script(
            artifact.content,
            gpu_instance=is_gpu_instance(job.instance_type),
            instances=job.instance_count,
        )
        for achado in achados:
            spec = _RULES.get(achado.rule_id)
            if spec is None:
                continue
            estimation = _estimation(achado.rule_id, job, config)
            # Estimativa indisponível não vira oportunidade de economia zero: um
            # achado permanentemente sem cifra é indistinguível de um cuja
            # economia é zero de verdade, e os dois pedem leituras opostas.
            tem_cifra = estimation is not None and estimation.saving_quality != "unavailable"
            if (
                achado.rule_id in _SELF_EVIDENT
                or _has_runtime_correlation(achado.rule_id, job)
            ) and tem_cifra:
                found.append(
                    _code_opportunity(
                        account, job, artifact, achado.lines, spec, achado.rule_id,
                        estimation, config, scan_id,
                    )
                )
            else:
                signals.append(
                    _code_signal(job, artifact, achado.lines, spec, achado.rule_id)
                )
    return found, signals


def _estimation(rule_id: str, job: SageMakerJob, config: Config):
    """A cifra existe para os padrões cujo alvo o motor sabe precificar.

    Trocar GPU por CPU e encolher um cluster têm alvo conhecido — tarifa de
    outra instância, número menor de instâncias — e a tabela da região responde
    por eles. Épocas a menos e modo de entrada diferente dependem de quanto o
    treino encurta, que só o piloto mede: esses vão como sinal, sempre.
    """
    if rule_id == "SM-CODE-CPU-ONLY-ON-GPU":
        return sm_est.gpu_to_cpu_saving(job, config)
    if rule_id == "SM-CODE-SINGLE-DEVICE-MULTI-INSTANCE":
        return sm_est.idle_instances_saving(job, config)
    return None


def _has_runtime_correlation(rule_id: str, job: SageMakerJob) -> bool:
    if rule_id == "SM-CODE-CPU-ONLY-ON-GPU":
        # A telemetria já era coletada e já decidia rightsizing; aqui ela
        # responde uma pergunta diferente: a GPU foi usada em algum momento?
        return job.detailed_metrics and job.gpu_p95 is not None and job.gpu_p95 < 5.0
    if rule_id == "SM-CODE-FULL-DATASET-LOAD":
        return _overhead_seconds(job) > 0
    return False


def _overhead_seconds(job: SageMakerJob) -> float:
    """Tempo cobrado que não foi treino: download de dado, warm pool, setup."""
    if job.billable_seconds is None or job.training_seconds is None:
        return 0.0
    return max(0.0, job.billable_seconds - job.training_seconds)


def _code_signal(
    job: SageMakerJob,
    artifact: CodeArtifact,
    lines: tuple[int, ...],
    spec: RuleSpec,
    rule_id: str,
) -> Signal:
    baseline = job.allocated_cost if job.allocated_cost is not None else job.modeled_cost
    return Signal(
        kind="code",
        rule_id=rule_id,
        asset_type=f"sagemaker_{job.kind}_job",
        asset_name=job.name,
        observation=spec.finding,
        question=(
            f"{spec.why} Confirme contra o script completo se o padrão custa "
            "capacidade neste job, ou descarte explicando por que ele é adequado aqui."
        ),
        missing_evidence=list(_missing(spec, artifact)),
        artifact_sha256=artifact.sha256,
        lines=list(lines),
        doc_links=[spec.doc],
        potential_range=potential(
            baseline,
            fraction=spec.fraction,
            basis=(
                "custo atribuído do job"
                if job.allocated_cost is not None
                else "custo modelado do job pela tarifa da instância"
            ),
            caveat=(
                "fração típica do padrão aplicada ao custo do job; só o piloto "
                "mede quanto a duração muda de fato"
            ),
        ),
    )


def _code_opportunity(
    account: Account,
    job: SageMakerJob,
    artifact: CodeArtifact,
    lines: tuple[int, ...],
    spec: RuleSpec,
    rule_id: str,
    estimation,
    config: Config,
    scan_id: str,
) -> Opportunity:
    evidencia = [
        # O `source` de um script mode é o `.tar.gz`; sem nomear o arquivo lido
        # dentro dele, o achado aponta para um pacote e não para um script.
        f"arquivo analisado={job.code_entry_point or artifact.source.rsplit('/', 1)[-1]}",
        f"script sha256={artifact.sha256[:16]}",
        f"linhas={','.join(str(line) for line in lines[:20]) or 'não disponível'}",
        f"artefato {'truncado' if artifact.truncated else 'completo'}",
        f"instância={job.instance_type} × {job.instance_count}",
    ]
    if job.gpu_p95 is not None:
        evidencia.append(f"GPU p95 observada={job.gpu_p95:.1f}%")
    if _overhead_seconds(job) > 0:
        evidencia.append(
            f"tempo cobrado fora do treino={_overhead_seconds(job):.0f}s"
        )
    opportunity = build(
        Finding(
            asset_type=f"sagemaker_{job.kind}_job",
            asset_name=job.name,
            rule_id=rule_id,
            rule_version="1.0.0",
            title=spec.finding,
            why=spec.why,
            source_process=job.pipeline_name or job.name,
        ),
        Recommendation(
            difficulty=spec.difficulty,
            action=spec.action,
            how_to_apply=spec.apply,
            how_to_validate=spec.validate,
            risks=[
                "alteração de código ou de instância exige comparar resultado do "
                "treino, não só duração e custo",
            ],
            docs=[spec.doc],
            risk=spec.risk,
            blocked=True,
        ),
        Evidence(
            items=evidencia,
            sources=[
                "SageMaker Describe*Job",
                "SageMaker sourcedir (S3)",
                "scanner estático Julius",
            ],
            observed_runs=job.workload_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=job.detailed_metrics and not artifact.truncated,
            owner_tag=job.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    opportunity.missing_evidence = list(_missing(spec, artifact))
    opportunity.next_action = spec.validate
    opportunity.evidence_refs.append(
        {
            "source": artifact.source or "SageMaker sourcedir",
            "sha256": artifact.sha256,
            "lines": list(lines),
        }
    )
    return opportunity


def _missing(spec: RuleSpec, artifact: CodeArtifact) -> list[str]:
    faltando = list(spec.missing)
    if artifact.truncated:
        faltando.append("script completo")
    return faltando
