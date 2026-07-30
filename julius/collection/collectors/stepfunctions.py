"""Coleta Step Functions e deriva chamadas a Glue Jobs da definição ASL.

A ASL diz o que a máquina *pode* fazer; só o histórico diz o que ela fez. Como
o Standard é cobrado por transição de estado, sem contar transição não há
baseline — e era isso que faltava: as regras existiam, o modelo financeiro
existia, e o coletor nunca preenchia `avg_state_transitions`, então a economia
saía zero e as regras não disparavam em conta real.

O histórico é amostrado, não varrido. `GetExecutionHistory` é uma chamada por
execução, e uma máquina de alto volume tem dezenas de milhares delas na janela;
o teto explícito segue a mesma disciplina dos Spark event logs. Amostra vazia
deixa o campo em `None` — evidência ausente nunca vira zero, porque zero
transições é uma afirmação, e uma afirmação falsa.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from julius.collection.collectors.paginate import safe_call, safe_pages
from julius.collection.health.recorder import error_category
from julius.collection.models import StateMachine
from julius.collection.window import AnalysisWindow

#: Execuções lidas por state machine. Suficiente para uma média estável de
#: transições sem transformar a coleta em varredura do histórico.
_MAX_SAMPLED_EXECUTIONS = 20


def collect_state_machines(
    client,
    *,
    cloudwatch_client=None,
    account_metrics: dict[str, int] | None = None,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[StateMachine]:
    """Máquinas de estado, isoladas uma a uma.

    `DescribeStateMachine` e `ListExecutions` são chamados por máquina, e sob
    política de recurso é comum uma delas ser negada. Antes essa negação subia
    do laço e a fonte inteira ficava vazia — o relatório passava a afirmar que a
    conta não usa Step Functions. Agora a máquina negada entra com o que o
    `ListStateMachines` trouxe e diz o que não foi lido.
    """
    cutoff = window.start
    months = max(1.0, window.days / 30.0)
    machines: list[StateMachine] = []

    listagem = safe_pages(client, "list_state_machines", "stateMachines")
    if gaps is not None and not listagem.complete:
        gaps.append(f"list_state_machines: {listagem.error_category or 'incompleto'}")

    for summary in listagem.items:
        arn = summary["stateMachineArn"]
        detail, falha_detalhe = safe_call(
            client, "describe_state_machine", stateMachineArn=arn
        )
        if falha_detalhe and gaps is not None:
            gaps.append(f"describe_state_machine: {falha_detalhe}")
        definition = _json(detail.get("definition", "{}"))
        executions, falha_execucoes = _executions(client, arn, cutoff)
        if falha_execucoes and gaps is not None:
            gaps.append(f"list_executions: {falha_execucoes}")
        durations = [
            (item["stopDate"] - item["startDate"]).total_seconds()
            for item in executions
            if item.get("stopDate") and item.get("startDate")
        ]
        loop_states = _polling_loop_states(definition)
        transitions, extra, retry_extra, failed_transitions = _sample_transitions(
            client, executions, loop_states
        )
        machines.append(
            StateMachine(
                name=summary["name"],
                arn=arn,
                type=detail.get("type", "STANDARD"),
                executions_per_month=round(len(executions) / months),
                avg_duration_sec=round(sum(durations) / len(durations), 1)
                if durations
                else 0.0,
                avg_state_transitions=transitions,
                poll_extra_transitions=extra,
                max_retry_attempts=_max_retry_attempts(definition),
                observed_runs=len(executions),
                coverage_days=window.days,
                sampled_executions=min(len(executions), _MAX_SAMPLED_EXECUTIONS),
                glue_jobs=sorted(_glue_jobs(definition)),
                has_polling_loop=bool(loop_states),
                definition_available=not falha_detalhe,
                execution_history_available=not falha_execucoes,
                # O coletor é read-only e não executa benchmark. Estes campos
                # só podem ser enriquecidos depois por evidência externa.
                express_benchmark_duration_ms=None,
                express_benchmark_memory_mb=None,
                failed_executions=sum(
                    item.get("status") == "FAILED" for item in executions
                ),
                timed_out_executions=sum(
                    item.get("status") == "TIMED_OUT" for item in executions
                ),
                aborted_executions=sum(
                    item.get("status") == "ABORTED" for item in executions
                ),
                avg_failed_state_transitions=failed_transitions,
                avg_retry_transitions=retry_extra,
                open_executions_max=sum(
                    item.get("status") == "RUNNING" for item in executions
                ),
            )
        )
    if cloudwatch_client is not None and machines:
        _enrich_cloudwatch(cloudwatch_client, machines, window, gaps)
        if account_metrics is not None:
            _account_cloudwatch(cloudwatch_client, account_metrics, window, gaps)
    return machines


def _sample_transitions(
    client, executions: list[dict], loop_states: set[str]
) -> tuple[int | None, int | None, int | None, int | None]:
    """Média de transições por execução, e quantas delas vêm do loop de espera.

    Devolve `(None, None)` quando não houve o que amostrar: sem histórico não
    existe contagem, e um zero aqui viraria baseline zero — que se lê como
    "esta máquina não custa nada".
    """
    sample = executions[:_MAX_SAMPLED_EXECUTIONS]
    entered: list[int] = []
    loop_entered: list[int] = []
    retry_entered: list[int] = []
    failed_entered: list[int] = []
    for execution in sample:
        arn = execution.get("executionArn")
        if not arn:
            continue
        events = _execution_events(client, arn)
        if events is None:
            continue
        states = [
            str(event["stateEnteredEventDetails"].get("name") or "")
            for event in events
            if event.get("type", "").endswith("StateEntered")
            and event.get("stateEnteredEventDetails")
        ]
        if not states:
            continue
        entered.append(len(states))
        loop_entered.append(sum(1 for name in states if name in loop_states))
        counts = Counter(states)
        retry_entered.append(sum(max(0, count - 1) for count in counts.values()))
        if execution.get("status") in {"FAILED", "TIMED_OUT", "ABORTED"}:
            failed_entered.append(len(states))
    if not entered:
        return None, None, None, None
    average = round(sum(entered) / len(entered))
    # As transições do loop só são "extras" além da primeira passagem: entrar
    # uma vez em cada estado do caminho é o trabalho, repetir é a espera.
    extra = round(sum(loop_entered) / len(loop_entered)) - len(loop_states)
    return (
        average,
        max(0, extra),
        round(sum(retry_entered) / len(retry_entered)),
        (
            round(sum(failed_entered) / len(failed_entered))
            if failed_entered
            else None
        ),
    )


_CW_METRICS = {
    "ExecutionsFailed": ("cw_failed_executions", "Sum"),
    "ExecutionsTimedOut": ("cw_timed_out_executions", "Sum"),
    "ExecutionsAborted": ("cw_aborted_executions", "Sum"),
    "ExecutionThrottled": ("throttled_events", "Sum"),
    "ExecutionsRedriven": ("redriven_executions", "Sum"),
    "ExecutionTime": ("duration_p95_ms", "p95"),
}


def _enrich_cloudwatch(client, machines, window, gaps) -> None:
    """Métricas operacionais best-effort; não substituem o histórico financeiro."""
    queries = []
    targets = {}
    for machine_index, machine in enumerate(machines):
        for metric_index, (metric, (field, stat)) in enumerate(_CW_METRICS.items()):
            query_id = f"m{machine_index}_{metric_index}"
            queries.append(
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/States",
                            "MetricName": metric,
                            "Dimensions": [
                                {"Name": "StateMachineArn", "Value": machine.arn}
                            ],
                        },
                        "Period": 86400,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
            targets[query_id] = (machine, field, stat)
    try:
        for offset in range(0, len(queries), 500):
            response = client.get_metric_data(
                MetricDataQueries=queries[offset : offset + 500],
                StartTime=window.start,
                EndTime=window.end,
                ScanBy="TimestampAscending",
            )
            for result in response.get("MetricDataResults", []):
                target = targets.get(result.get("Id"))
                values = result.get("Values") or []
                if target is None or not values:
                    continue
                machine, field, stat = target
                value = max(values) if stat in {"Maximum", "p95"} else sum(values)
                setattr(
                    machine,
                    field,
                    round(value, 2) if stat == "p95" else int(value),
                )
    except Exception as exc:
        if gaps is not None:
            gaps.append(f"cloudwatch_stepfunctions: {error_category(exc)}")


def _account_cloudwatch(client, target, window, gaps) -> None:
    """Métricas sem dimensão de state machine permanecem no nível da conta."""
    for metric, field in (
        ("ApproximateMapRunBacklogSize", "map_backlog"),
        ("OpenExecutionCount", "open_executions"),
    ):
        try:
            response = client.get_metric_statistics(
                Namespace="AWS/States",
                MetricName=metric,
                StartTime=window.start,
                EndTime=window.end,
                Period=86400,
                Statistics=["Maximum"],
            )
            values = [
                int(point.get("Maximum") or 0)
                for point in response.get("Datapoints", [])
            ]
            target[field] = max(values, default=0)
        except Exception as exc:
            if gaps is not None:
                gaps.append(f"cloudwatch_stepfunctions_account: {error_category(exc)}")
            return
    if not hasattr(client, "list_metrics"):
        return
    for metric, field in (
        ("ServiceIntegrationsFailed", "service_integration_failures"),
        ("ServiceIntegrationsTimedOut", "service_integration_timeouts"),
    ):
        target[field] = _aggregate_dimensioned_metric(
            client, metric, window, gaps
        )


def _aggregate_dimensioned_metric(client, metric_name, window, gaps) -> int:
    """Soma até 100 recursos; a dimensão oficial é o ARN integrado, não a máquina."""
    metrics: list[dict] = []
    token = None
    try:
        while len(metrics) < 100:
            kwargs = {"Namespace": "AWS/States", "MetricName": metric_name}
            if token:
                kwargs["NextToken"] = token
            response = client.list_metrics(**kwargs)
            metrics.extend(response.get("Metrics", []))
            token = response.get("NextToken")
            if not token:
                break
    except Exception as exc:
        if gaps is not None:
            gaps.append(f"cloudwatch_stepfunctions_integrations: {error_category(exc)}")
        return 0
    queries = [
        {
            "Id": f"i{index}",
            "MetricStat": {
                "Metric": metric,
                "Period": 86400,
                "Stat": "Sum",
            },
            "ReturnData": True,
        }
        for index, metric in enumerate(metrics[:100])
    ]
    if not queries:
        return 0
    try:
        response = client.get_metric_data(
            MetricDataQueries=queries,
            StartTime=window.start,
            EndTime=window.end,
            ScanBy="TimestampAscending",
        )
    except Exception as exc:
        if gaps is not None:
            gaps.append(f"cloudwatch_stepfunctions_integrations: {error_category(exc)}")
        return 0
    if token and gaps is not None:
        gaps.append("cloudwatch_stepfunctions_integrations: bounded_or_incomplete")
    return round(
        sum(
            sum(result.get("Values") or [])
            for result in response.get("MetricDataResults", [])
        )
    )


def _execution_events(client, execution_arn: str) -> list[dict] | None:
    try:
        response = client.get_execution_history(
            executionArn=execution_arn,
            includeExecutionData=False,
        )
    except Exception:
        return None
    events = response.get("events")
    return events if isinstance(events, list) else None


def _max_retry_attempts(definition: dict) -> int:
    """Maior `MaxAttempts` declarado em qualquer Retry da definição."""
    attempts = 0
    for state in _states(definition):
        for retry in state.get("Retry", []) or []:
            if isinstance(retry, dict):
                attempts = max(attempts, int(retry.get("MaxAttempts", 3) or 0))
    return attempts


def _executions(client, arn: str, cutoff: datetime) -> tuple[list[dict], str]:
    """Execuções dentro da janela, e a categoria do erro se houver.

    Não usa `safe_pages` porque a saída antecipada é o que limita o custo: a API
    devolve o histórico do mais recente para o mais antigo, e parar na primeira
    execução anterior à janela evita paginar anos de histórico. Aqui o `try`
    envolve a iteração — que é onde a chamada HTTP acontece.
    """
    executions: list[dict] = []
    try:
        pages = client.get_paginator("list_executions").paginate(stateMachineArn=arn)
        for page in pages:
            for item in page.get("executions", []):
                started = item.get("startDate")
                if started and started.replace(tzinfo=started.tzinfo or timezone.utc) < cutoff:
                    return executions, ""
                executions.append(item)
    except Exception as exc:
        return executions, error_category(exc)
    return executions, ""


def _json(value: str) -> dict:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _states(definition: dict):
    for state in (definition.get("States") or {}).values():
        yield state
        for branch in state.get("Branches", []) or []:
            yield from _states(branch)
        if state.get("ItemProcessor"):
            yield from _states(state["ItemProcessor"])
        if state.get("Iterator"):
            yield from _states(state["Iterator"])


def _glue_jobs(definition: dict) -> set[str]:
    jobs: set[str] = set()
    for state in _states(definition):
        resource = str(state.get("Resource", "")).lower()
        if "glue:startjobrun" not in resource:
            continue
        params = state.get("Parameters", {}) or {}
        name = params.get("JobName") or params.get("JobName.$")
        if name and not str(name).startswith("$"):
            jobs.add(str(name))
    return jobs


def _polling_loop_states(definition: dict) -> set[str]:
    """Os estados que formam o ciclo `Wait → … → Wait`, quando ele existe.

    Antes bastava saber que o loop existia. Agora o conjunto importa: é ele que
    permite contar, no histórico, quantas transições vieram de espera e quantas
    do trabalho em si.
    """
    states = definition.get("States", {}) or {}
    for name, state in states.items():
        if state.get("Type") != "Wait":
            continue
        path: list[str] = [name]
        seen: set[str] = {name}
        current = state.get("Next")
        while current and current not in seen and current in states:
            seen.add(current)
            path.append(current)
            candidate = states[current]
            if candidate.get("Next") == name:
                return set(path)
            current = candidate.get("Next")
    return set()
