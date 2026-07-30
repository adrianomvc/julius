"""Detectores de Step Functions: Standard→Express e loop de polling."""

from __future__ import annotations

from julius.collection.models import Account, StateMachine
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal
from julius.knowledge.rules.stepfunctions import estimation as sfn_est

_DOC_EXPRESS = (
    "https://docs.aws.amazon.com/step-functions/latest/dg/concepts-standard-vs-express.html"
)
_DOC_SYNC = "https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html"
_DOC_RETRY = (
    "https://docs.aws.amazon.com/step-functions/latest/dg/concepts-error-handling.html"
)
_DOC_MONITOR = (
    "https://docs.aws.amazon.com/step-functions/latest/dg/procedure-cw-metrics.html"
)


def _express_candidate(sm: StateMachine, th) -> bool:
    """O que é fato: tipo declarado, volume medido, duração medida.

    A idempotência ficava aqui e matava a regra — o coletor nunca preencheu o
    campo, então `is True` nunca era verdade em conta real. Ela saiu porque
    tolerar semântica at-least-once é propriedade da lógica de negócio, não da
    configuração: vira sinal para a análise contextual julgar contra a ASL.
    """
    return (
        sm.type == "STANDARD"
        and sm.executions_per_month >= th.sfn_express_min_executions
        and 0 < sm.avg_duration_sec <= th.sfn_short_duration_sec
    )


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for sm in account.state_machines:
        if (
            _express_candidate(sm, th)
            and sm.idempotent is True
            and sm.express_benchmark_duration_ms is not None
            and sm.express_benchmark_memory_mb is not None
            and sm.avg_state_transitions is not None
        ):
            out.append(_to_express(account, sm, config, scan_id))

        if sm.type == "STANDARD" and sm.has_polling_loop:
            out.append(_polling(account, sm, config, scan_id))
        if sm.type == "STANDARD" and sm.failed_executions and sm.avg_failed_state_transitions:
            out.append(_transition_waste(account, sm, config, scan_id, failed=True))
        if (
            sm.type == "STANDARD"
            and sm.max_retry_attempts > 0
            and sm.avg_retry_transitions
        ):
            out.append(_transition_waste(account, sm, config, scan_id, failed=False))
    return out


def _transition_waste(
    account: Account,
    sm: StateMachine,
    config: Config,
    scan_id: str,
    *,
    failed: bool,
) -> Opportunity:
    if failed:
        rule_id = "SFN-FAILED-TRANSITION-COST"
        transitions = sm.failed_executions * (sm.avg_failed_state_transitions or 0)
        title = "Falhas consomem transições cobradas"
        action = "Corrigir a causa das execuções com falha"
        why = (
            f"{sm.failed_executions} falhas observadas, com média de "
            f"{sm.avg_failed_state_transitions} transições até falhar."
        )
    else:
        rule_id = "SFN-RETRY-WASTE"
        transitions = sm.observed_runs * (sm.avg_retry_transitions or 0)
        title = "Retries repetem transições cobradas"
        action = "Corrigir a falha recorrente antes de ampliar retries"
        why = (
            f"{sm.avg_retry_transitions} transições repetidas por execução na "
            f"amostra de {sm.sampled_executions} execuções."
        )
    cost = transitions * config.pricing.sfn_standard_per_transition
    return build(
        Finding(
            asset_type="state_machine",
            asset_name=sm.name,
            rule_id=rule_id,
            rule_version="1.0.0",
            title=title,
            why=why,
        ),
        Recommendation(
            difficulty=2,
            action=action,
            how_to_apply=(
                "Investigar a integração/Task responsável e validar a correção "
                "em ambiente controlado; o Julius não altera a state machine."
            ),
            how_to_validate="Comparar falhas, retries e transições na janela seguinte.",
            risks=["reduzir retry sem corrigir a causa pode diminuir resiliência"],
            docs=[_DOC_RETRY, _DOC_MONITOR],
        ),
        Evidence(
            items=[why, f"{transitions} transições evitáveis na amostra/janela"],
            sources=["States ListExecutions", "States GetExecutionHistory"],
            observed_runs=sm.sampled_executions,
            coverage_days=sm.coverage_days,
            has_optional_metrics=True,
            owner_tag=sm.owner_tag,
        ),
        Estimation(
            method=rule_id.lower().replace("-", "_") + "_v1",
            baseline_cost=round(cost, 2),
            projected_cost=0.0,
            estimated_saving=round(cost, 2),
            assumptions=[
                "somente transições observadas no histórico amostrado",
                "CloudWatch é contexto best-effort, não fonte do cálculo",
            ],
            pricing_region=config.pricing.region,
            estimation_version=config.pricing.version,
            baseline_quality="modeled",
            saving_quality="measured",
        ),
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _to_express(account: Account, sm: StateMachine, config: Config, scan_id: str) -> Opportunity:
    est = sfn_est.standard_to_express_saving(sm, config)
    opportunity = build(
        Finding(
            asset_type="state_machine",
            asset_name=sm.name,
            rule_id="SFN-STANDARD-TO-EXPRESS",
            rule_version="1.0.0",
            title="Standard Workflow candidato a Express",
            why=(
                f"{sm.executions_per_month} execuções/mês curtas "
                f"({sm.avg_duration_sec:.0f}s). O contrafactual inclui requests, "
                "duração e memória medidos em benchmark."
            ),
        ),
        Recommendation(
            difficulty=3,
            action="Avaliar migração de Standard para Express",
            how_to_apply="Recriar a state machine como EXPRESS; validar idempotência e o limite de 5 min.",
            how_to_validate="Comparar custo (transições × execuções) antes × depois.",
            risks=["Express é at-least-once", "limite de 5 min por execução"],
            docs=[_DOC_EXPRESS],
            risk=0.6,
            # A migração exige alguém afirmar que a carga é idempotente, e isso
            # não sai da configuração. Quando a afirmação já existe — declarada
            # no dataset ou vinda de um veredito — ela vale; o que não vale é
            # tratar a ausência dela como permissão.
            blocked=False,
        ),
        Evidence(
            items=[
                f"type=STANDARD, {sm.executions_per_month} exec/mês",
                (
                    f"{sm.avg_state_transitions} transições/execução medidas em "
                    f"{sm.sampled_executions} execuções"
                    if sm.avg_state_transitions is not None
                    else "transições por execução não medidas"
                ),
                f"duração média {sm.avg_duration_sec:.0f}s",
            ],
            sources=[
                "States DescribeStateMachine",
                "States GetExecutionHistory (amostra)",
            ],
            observed_runs=sm.observed_runs,
            coverage_days=sm.coverage_days,
            has_optional_metrics=sm.avg_state_transitions is not None,
            owner_tag=sm.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )
    opportunity.missing_evidence = []
    if sm.avg_state_transitions is None:
        opportunity.missing_evidence.append(
            "contagem de transições por execução no histórico"
        )
    return opportunity


def _polling(account: Account, sm: StateMachine, config: Config, scan_id: str) -> Opportunity:
    est = sfn_est.polling_loop_saving(sm, config)
    opportunity = build(
        Finding(
            asset_type="state_machine",
            asset_name=sm.name,
            rule_id="SFN-POLLING-LOOP",
            rule_version="1.0.0",
            title="Loop de polling gera transições extras",
            why=(
                "Loop Wait→Task→Choice→Wait "
                + (
                    f"adiciona {sm.poll_extra_transitions} transições/execução"
                    if sm.poll_extra_transitions is not None
                    else "detectado na ASL, com custo por execução ainda não medido"
                )
                + " — evitável com .sync/callback."
            ),
        ),
        Recommendation(
            difficulty=2,
            action="Trocar polling por integração .sync ou callback (Task Token)",
            how_to_apply="Usar o padrão .sync do Glue/Athena ou waitForTaskToken em vez do loop de espera.",
            how_to_validate="Comparar nº de transições por execução antes × depois.",
            risks=["exige ajuste no fluxo"],
            docs=[_DOC_SYNC],
            # Sem contagem no histórico o loop é estrutura, não custo medido.
            blocked=sm.poll_extra_transitions is None,
        ),
        Evidence(
            items=[
                "padrão Wait→Task→Choice→Wait detectado na ASL",
                (
                    f"{sm.poll_extra_transitions} transições extras/execução "
                    f"medidas em {sm.sampled_executions} execuções"
                    if sm.poll_extra_transitions is not None
                    else "transições de espera não contadas: histórico não amostrado"
                ),
                f"{sm.executions_per_month} execuções/mês",
            ],
            sources=[
                "States DescribeStateMachine (ASL)",
                "States GetExecutionHistory (amostra)",
            ],
            observed_runs=sm.observed_runs,
            coverage_days=sm.coverage_days,
            has_optional_metrics=sm.poll_extra_transitions is not None,
            owner_tag=sm.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )
    if sm.poll_extra_transitions is None:
        opportunity.missing_evidence = [
            "contagem das transições de espera no histórico de execução",
        ]
    return opportunity


def signals(account: Account, config: Config) -> list[Signal]:
    """O que a ASL levanta e a configuração não decide.

    Os dois casos aqui são números que o Julius mede sem saber o que
    significam. Se a carga tolera reexecução é propriedade da lógica de
    negócio; se um retry alto é resiliência ou máscara de falha depende do que
    o Task faz quando repete.
    """
    out: list[Signal] = []
    th = config.thresholds
    for sm in account.state_machines:
        if _express_candidate(sm, th) and (
            sm.idempotent is not True
            or sm.express_benchmark_duration_ms is None
            or sm.express_benchmark_memory_mb is None
            or sm.avg_state_transitions is None
        ):
            out.append(
                Signal(
                    kind="config",
                    rule_id="SFN-STANDARD-TO-EXPRESS",
                    asset_type="state_machine",
                    asset_name=sm.name,
                    observation=(
                        f"'{sm.name}' é STANDARD com {sm.executions_per_month} "
                        f"execuções/mês de ~{sm.avg_duration_sec:.0f}s — perfil de "
                        "Express, cuja semântica é at-least-once."
                    ),
                    question=(
                        "A ASL tolera execução repetida? Há Task com efeito "
                        "colateral não idempotente — escrita sem chave de "
                        "deduplicação, envio de notificação, cobrança — que a "
                        "reexecução do Express duplicaria?"
                    ),
                    missing_evidence=[
                        *(
                            ["confirmação de idempotência dos Tasks com efeito colateral"]
                            if sm.idempotent is not True
                            else []
                        ),
                        *(
                            ["contagem de transições por execução"]
                            if sm.avg_state_transitions is None
                            else []
                        ),
                        *(
                            ["benchmark externo de duração e memória do Express"]
                            if sm.express_benchmark_duration_ms is None
                            or sm.express_benchmark_memory_mb is None
                            else []
                        ),
                    ],
                    doc_links=[_DOC_EXPRESS],
                )
            )
        if sm.max_retry_attempts >= th.sfn_retry_attempts_high:
            out.append(
                Signal(
                    kind="config",
                    rule_id="SFN-RETRY-MASKING",
                    asset_type="state_machine",
                    asset_name=sm.name,
                    observation=(
                        f"Retry declara até {sm.max_retry_attempts} tentativas em "
                        f"'{sm.name}'."
                    ),
                    question=(
                        "O retry cobre falha transitória de verdade, ou mascara "
                        "erro recorrente e repaga o trabalho já cobrado a cada "
                        "tentativa?"
                    ),
                    missing_evidence=[
                        "taxa de falha por Task e quantas execuções chegam ao "
                        "último retry",
                    ],
                    doc_links=[_DOC_RETRY],
                )
            )
        operational = (
            ("SFN-REDRIVE-REPROCESSING", sm.redriven_executions, "redrives"),
            ("SFN-EXECUTION-THROTTLING", sm.throttled_events, "throttles"),
            (
                "SFN-STUCK-OPEN-EXECUTIONS",
                sm.open_executions_max,
                "execuções abertas no pico",
            ),
        )
        for rule_id, value, label in operational:
            if value <= 0:
                continue
            out.append(
                Signal(
                    kind="metric",
                    rule_id=rule_id,
                    asset_type="state_machine",
                    asset_name=sm.name,
                    observation=f"{value} {label} observados no CloudWatch.",
                    question="Qual causa operacional está gerando reprocessamento ou espera?",
                    missing_evidence=[
                        "histórico de execução para atribuir transições e custo"
                    ],
                    doc_links=[_DOC_MONITOR],
                )
            )
    for rule_id, value, label in (
        (
            "SFN-DISTRIBUTED-MAP-BACKLOG",
            account.stepfunctions_map_backlog,
            "Map Runs em backlog",
        ),
        (
            "SFN-STUCK-OPEN-EXECUTIONS",
            account.stepfunctions_open_executions,
            "execuções abertas no pico da conta",
        ),
        (
            "SFN-SERVICE-INTEGRATION-FAILURE",
            account.stepfunctions_service_integration_failures,
            "falhas em integrações de serviço",
        ),
        (
            "SFN-SERVICE-INTEGRATION-TIMEOUT",
            account.stepfunctions_service_integration_timeouts,
            "timeouts em integrações de serviço",
        ),
    ):
        if value:
            out.append(
                Signal(
                    kind="metric",
                    rule_id=rule_id,
                    asset_type="aws_account",
                    asset_name=account.account_id,
                    observation=f"{value} {label}.",
                    question="Quais state machines explicam este indicador da conta?",
                    missing_evidence=["atribuição por execução/state machine"],
                    doc_links=[_DOC_MONITOR],
                )
            )
    return out
