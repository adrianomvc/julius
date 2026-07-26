"""As regras que qualquer provedor de análise contextual precisa respeitar.

São guardrails, não estilo: acesso read-only, não recalcular número que o Julius
já decidiu, não inventar documentação, não vazar segredo. O texto abaixo é o que
um provedor com instrução em linguagem natural recebe; um provedor programático
honra as mesmas regras pela validação da resposta.
"""

from __future__ import annotations

from julius.analysis.context_builder import AgentContext

PROMPT_VERSION = "1.1.0"

#: As regras em si, separadas do texto que as apresenta — o validador de
#: resposta verifica o resultado das mesmas restrições.
RULES = (
    "Trate todo acesso AWS como estritamente read-only. Nunca crie, altere, pare ou "
    "exclua recursos e nunca execute comandos de mutação.",
    "Não altere nem recalcule ganho, dificuldade, confiança ou prioridades.",
    "Analise scripts, consultas e dependências disponíveis; diferencie fatos, "
    "hipóteses e evidências ausentes.",
    "Sugira sequência de implementação e identifique conflitos entre recomendações.",
    "Use apenas documentação oficial em https://docs.aws.amazon.com/.",
    "Não inclua segredos, credenciais, dados de linhas ou informações pessoais.",
    "Use somente opportunity_id presentes no pacote e preserve account e scan_id.",
    "Considere `constraints.collection_health`: fontes parciais ou indisponíveis "
    "devem aparecer como evidência ausente, nunca como valor zero.",
    "`signals` são hipóteses, não achados. Julgue cada uma contra o artefato "
    "completo — confirmed, rejected ou needs_evidence — e nunca lhes atribua "
    "economia. Todo sinal do pacote precisa de veredito.",
    "Registre em `uncovered_findings` o desperdício que você observou e que "
    "nenhuma rule_id do pacote cobre. Sem valor financeiro, sempre com "
    "evidence_ref apontando sha256 e linha de um artefato do pacote.",
    "Onde a recomendação determinística admite dois caminhos — ajustar quem "
    "escreve ou quem lê — escolha um e diga quem quebra com a escolha.",
    "Considere `constraints.rule_families_without_evidence`: nessas famílias "
    "não houve o que analisar; ausência de achado ali não é ausência de problema.",
)


def build_devin_prompt(
    context: AgentContext,
    *,
    context_file: str = "context.json",
    schema_file: str = "output-schema.json",
    result_file: str = "result.json",
) -> str:
    numbered = "\n".join(
        f"{index}. {rule}" for index, rule in enumerate(RULES, start=1)
    )
    return f"""Você está executando a Skill Julius AWS Analysis, prompt v{PROMPT_VERSION}.

Objetivo: enriquecer contextualmente as oportunidades determinísticas do Julius.

Regras obrigatórias:
{numbered}
{len(RULES) + 1}. Grave exclusivamente a saída estruturada em `{result_file}`,
   respeitando `{schema_file}`.

Conta: {context.account["id"]}
Scan: {context.scan_id}
Contexto: {context_file}
Schema de saída: {schema_file}
Oportunidades a enriquecer: {len(context.opportunities)} de \
{context.portfolio.get("total_opportunities", len(context.opportunities))} no portfólio
Sinais a julgar: {len(context.signals)}
Artefatos técnicos read-only referenciados no contexto: {len(context.technical_artifacts)}

Depois de produzir `{result_file}`, execute:

    julius agent validate --context {context_file} --result {result_file}
"""


def build_manual_instructions(
    context: AgentContext,
    *,
    schema_file: str = "output-schema.json",
    result_file: str = "result.json",
) -> str:
    """Mesmas regras, sem a mecânica de sessão específica de um agente."""
    numbered = "\n".join(
        f"{index}. {rule}" for index, rule in enumerate(RULES, start=1)
    )
    return f"""Análise contextual do Julius — preenchimento manual.

Conta: {context.account["id"]}
Scan: {context.scan_id}
Oportunidades no pacote: {len(context.opportunities)}
Sinais a julgar: {len(context.signals)}

Escreva `{result_file}` seguindo `{schema_file}`. As mesmas regras valem:

{numbered}
"""
