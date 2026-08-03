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
from julius.knowledge.signal_potential import potential

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

    `express_blockers` entrou depois, e é o oposto: também vem da definição, mas
    não pede julgamento nenhum. `.sync`, `waitForTaskToken` e timeout acima de
    cinco minutos são recusados pela API do Express. Perguntar a alguém se
    migra uma máquina que não pode migrar gasta a credibilidade do relatório
    inteiro por um candidato que nunca ia dar em nada.
    """
    return (
        sm.type == "STANDARD"
        and not sm.express_blockers
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
        if (
            sm.type == "STANDARD"
            and _execucoes_perdidas(sm)
            and sm.avg_failed_state_transitions
        ):
            out.append(_transition_waste(account, sm, config, scan_id, failed=True))
        if (
            sm.type == "STANDARD"
            and sm.max_retry_attempts > 0
            and sm.avg_retry_transitions
        ):
            out.append(_transition_waste(account, sm, config, scan_id, failed=False))
    return out


def _execucoes_perdidas(sm: StateMachine) -> int:
    """Execuções que gastaram transição e não entregaram resultado.

    A regra contava só `FAILED` e subestimava a própria cifra. Timeout e abort
    param a máquina no meio exatamente como a falha: as transições até ali já
    foram cobradas, e o trabalho terá de ser refeito. Os três estados vêm do
    mesmo `list_executions`, sobre a mesma janela, então somá-los não mistura
    fontes nem períodos.
    """
    return sm.failed_executions + sm.timed_out_executions + sm.aborted_executions


def _composicao_perdidas(sm: StateMachine) -> str:
    """Quais estados formam o total — um timeout não se corrige como uma falha."""
    partes = [
        f"{valor} {rotulo}"
        for valor, rotulo in (
            (sm.failed_executions, "com falha"),
            (sm.timed_out_executions, "por timeout"),
            (sm.aborted_executions, "abortadas"),
        )
        if valor
    ]
    return ", ".join(partes)


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
        perdidas = _execucoes_perdidas(sm)
        transitions = perdidas * (sm.avg_failed_state_transitions or 0)
        title = "Execuções que não completam consomem transições cobradas"
        action = "Corrigir a causa das execuções que não completam"
        why = (
            f"{perdidas} execuções não completaram ({_composicao_perdidas(sm)}), "
            f"com média de {sm.avg_failed_state_transitions} transições até parar."
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


def _efeitos_externos(sm: StateMachine) -> str:
    """Os estados de efeito externo, anexados à pergunta que fala deles.

    A pergunta sobre idempotência já existia e era feita no vácuo: quem
    respondesse precisava abrir a definição e procurar sozinho. Nomear os
    estados não responde a pergunta — nada estático responde — mas transforma
    "procure um efeito colateral" em "olhe estes três".
    """
    estados = sm.asl_patterns.get("external_side_effect") or []
    if not estados:
        return ""
    return " Efeito externo sem deduplicação declarada em: " + ", ".join(estados) + "."


#: O que cada padrão da ASL pergunta, e quando perguntá-lo. A condição existe
#: porque veredito custa: o validador exige resposta para todo sinal do pacote,
#: e repetir uma pergunta que outra regra já faz gasta a análise no que já foi
#: perguntado em vez do que ninguém viu.
_ASL_SIGNALS = {
    "manual_polling": (
        "SFN-ASL-MANUAL-POLLING",
        "Integração com padrão .sync disponível é consultada em laço de espera: {estados}.",
        "O laço existe por limitação real da integração, ou por hábito onde "
        ".sync ou callback serviria? Cada volta do laço é transição cobrada.",
        ["transições gastas em espera versus em trabalho"],
        # `has_polling_loop` só reconhece o ciclo `Wait → … → Wait` no nível de
        # topo. Quando ele reconhece, a oportunidade já tem cifra e dizer o
        # mesmo sem número seria ruído; quando não, este sinal é o único que vê.
        lambda sm: not sm.has_polling_loop,
    ),
    "retry_unbounded": (
        "SFN-ASL-RETRY-UNBOUNDED",
        "Retry declara cinco ou mais tentativas sem espaçamento em: {estados}.",
        "A falha que dispara este retry é transitória? Sem backoff as "
        "tentativas repetem o trabalho já cobrado em sequência imediata.",
        ["taxa de falha por Task e quantas execuções chegam ao último retry"],
        # `SFN-RETRY-MASKING` já pergunta sobre retry alto. Duas perguntas sobre
        # retry na mesma máquina consomem dois vereditos e rendem um.
        lambda sm: sm.max_retry_attempts < 5,
    ),
    "catch_swallow": (
        "SFN-ASL-CATCH-SWALLOW",
        "Catch conduz direto a Succeed em: {estados}.",
        "A execução termina como sucesso mesmo quando o Task falha. Isso é "
        "tratamento deliberado, ou esconde falha que alguém reprocessa depois?",
        ["execuções que terminaram OK sem produzir a saída esperada"],
        lambda sm: True,
    ),
    "unbounded_fanout": (
        "SFN-ASL-UNBOUNDED-FANOUT",
        "Map sem MaxConcurrency em: {estados}.",
        "O paralelismo sem teto é intencional? Ele multiplica transições "
        "cobradas e é a origem do backlog que a conta observa depois.",
        ["itens por Map Run e pico de execuções simultâneas"],
        lambda sm: True,
    ),
}


#: Fração das transições da máquina que cada padrão pode devolver. Ordena
#: hipóteses entre si; não promete economia — ver `signal_potential`.
_ASL_FRACTIONS = {
    "manual_polling": 0.30,
    "retry_unbounded": 0.15,
    "catch_swallow": 0.10,
    "unbounded_fanout": 0.10,
}


def _transition_baseline(sm: StateMachine, config: Config) -> float | None:
    """Custo mensal de transição da máquina — medido, não estimado.

    `executions_per_month` vem do histórico e `avg_state_transitions` da amostra
    de `GetExecutionHistory`. Sem a segunda não há base: zero transições é uma
    afirmação, e uma afirmação falsa.
    """
    if sm.type != "STANDARD" or not sm.avg_state_transitions:
        return None
    return (
        sm.executions_per_month
        * sm.avg_state_transitions
        * config.pricing.sfn_standard_per_transition
    )


def _asl_signals(sm: StateMachine, config: Config) -> list[Signal]:
    """O que a definição levanta e o histórico não confirma.

    Nenhum destes vira cifra. O Standard cobra por transição, então a ASL diz
    quantas transições a máquina *pode* gastar — mas quantas ela gasta de fato
    está no histórico, e é de lá que as regras com número já tiram o baseline.
    """
    out: list[Signal] = []
    baseline = _transition_baseline(sm, config)
    for padrao, (rule_id, texto, pergunta, falta, quando) in _ASL_SIGNALS.items():
        estados = sm.asl_patterns.get(padrao) or []
        if not estados or not quando(sm):
            continue
        out.append(
            Signal(
                kind="code",
                rule_id=rule_id,
                asset_type="state_machine",
                asset_name=sm.name,
                observation=texto.format(estados=", ".join(estados)),
                question=pergunta,
                missing_evidence=list(falta),
                doc_links=[_DOC_RETRY if "RETRY" in rule_id else _DOC_SYNC],
                potential_range=potential(
                    baseline,
                    fraction=_ASL_FRACTIONS[padrao],
                    basis="transições/mês observadas × tarifa Standard",
                    caveat=(
                        "quantas transições o padrão responde só o histórico "
                        "por estado diria; a fração é típica, não medida"
                    ),
                ),
            )
        )
    return out


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
                        + _efeitos_externos(sm)
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
        out += _asl_signals(sm, config)
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
