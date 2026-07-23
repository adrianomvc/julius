"""Instruções versionadas lidas pelo Devin dentro do próprio workspace."""

from __future__ import annotations

from julius.agent.context import AgentContext

PROMPT_VERSION = "1.0.0"


def build_devin_prompt(
    context: AgentContext,
    *,
    context_file: str = "context.json",
    schema_file: str = "output-schema.json",
    result_file: str = "result.json",
) -> str:
    return f"""Você está executando a Skill Julius AWS Analysis, prompt v{PROMPT_VERSION}.

Objetivo: enriquecer contextualmente as oportunidades determinísticas do Julius.

Regras obrigatórias:
1. Trate todo acesso AWS como estritamente read-only. Nunca crie, altere, pare ou
   exclua recursos e nunca execute comandos de mutação.
2. Não altere nem recalcule ganho, dificuldade, confiança ou prioridades.
3. Analise scripts, consultas e dependências disponíveis; diferencie fatos,
   hipóteses e evidências ausentes.
4. Sugira sequência de implementação e identifique conflitos entre recomendações.
5. Use apenas documentação oficial em https://docs.aws.amazon.com/.
6. Não inclua segredos, credenciais, dados de linhas ou informações pessoais.
7. Grave exclusivamente a saída estruturada em `{result_file}`, respeitando
   `{schema_file}`.
8. Use somente opportunity_id presentes no pacote e preserve account e scan_id.

Conta: {context.account["id"]}
Scan: {context.scan_id}
Contexto: {context_file}
Schema de saída: {schema_file}

Depois de produzir `{result_file}`, execute:

    julius agent validate --context {context_file} --result {result_file}
"""
