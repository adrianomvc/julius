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
from datetime import datetime, timezone

from julius.collection.models import StateMachine
from julius.collection.window import AnalysisWindow

#: Execuções lidas por state machine. Suficiente para uma média estável de
#: transições sem transformar a coleta em varredura do histórico.
_MAX_SAMPLED_EXECUTIONS = 20


def collect_state_machines(
    client,
    *,
    window: AnalysisWindow,
) -> list[StateMachine]:
    cutoff = window.start
    months = max(1.0, window.days / 30.0)
    machines: list[StateMachine] = []

    paginator = client.get_paginator("list_state_machines")
    for page in paginator.paginate():
        for summary in page.get("stateMachines", []):
            arn = summary["stateMachineArn"]
            detail = client.describe_state_machine(stateMachineArn=arn)
            definition = _json(detail.get("definition", "{}"))
            executions = _executions(client, arn, cutoff)
            durations = [
                (item["stopDate"] - item["startDate"]).total_seconds()
                for item in executions
                if item.get("stopDate") and item.get("startDate")
            ]
            loop_states = _polling_loop_states(definition)
            transitions, extra = _sample_transitions(
                client, executions, loop_states
            )
            machines.append(
                StateMachine(
                    name=summary["name"],
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
                )
            )
    return machines


def _sample_transitions(
    client, executions: list[dict], loop_states: set[str]
) -> tuple[int | None, int | None]:
    """Média de transições por execução, e quantas delas vêm do loop de espera.

    Devolve `(None, None)` quando não houve o que amostrar: sem histórico não
    existe contagem, e um zero aqui viraria baseline zero — que se lê como
    "esta máquina não custa nada".
    """
    sample = executions[:_MAX_SAMPLED_EXECUTIONS]
    entered: list[int] = []
    loop_entered: list[int] = []
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
    if not entered:
        return None, None
    average = round(sum(entered) / len(entered))
    # As transições do loop só são "extras" além da primeira passagem: entrar
    # uma vez em cada estado do caminho é o trabalho, repetir é a espera.
    extra = round(sum(loop_entered) / len(loop_entered)) - len(loop_states)
    return average, max(0, extra)


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


def _executions(client, arn: str, cutoff: datetime) -> list[dict]:
    executions: list[dict] = []
    paginator = client.get_paginator("list_executions")
    for page in paginator.paginate(stateMachineArn=arn):
        for item in page.get("executions", []):
            started = item.get("startDate")
            if started and started.replace(tzinfo=started.tzinfo or timezone.utc) < cutoff:
                return executions
            executions.append(item)
    return executions


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
