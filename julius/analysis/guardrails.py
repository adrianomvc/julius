"""As regras e o escopo que qualquer provedor de análise contextual recebe.

Duas coisas moram aqui, e são diferentes. `RULES` são guardrails — acesso
read-only, não recalcular número que o Julius já decidiu, não inventar
documentação, não vazar segredo — e cada uma tem contrapartida no validador de
resposta. `DETERMINISTIC` é o oposto: não restringe, orienta. Diz o que o Julius
já resolveu sozinho, para o provedor não repetir.

A separação importa porque um provedor que só recebe proibição produz resposta
defensiva e vazia. Ele passa na validação e não acrescenta nada.

O que procurar em cada tipo de ativo era uma terceira tupla aqui, `SCOPE`, e ia
inteira em todo pacote. Agora está em `playbook.py`, carregada só para os ativos
que o pacote contém.
"""

from __future__ import annotations

from julius.analysis.context_builder import AgentContext
from julius.analysis.playbook import asset_types_in_context
from julius.analysis.playbook import render as render_playbooks

PROMPT_VERSION = "2.0.0"

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
    "Em um sinal confirmed você pode propor uma ação e um método de cálculo "
    "permitido, mas não invente custo nem economia: o motor determinístico "
    "resolve o ativo, valida o alvo e executa a fórmula. A estimativa contextual "
    "nunca entra no portfólio.",
    "Registre em `uncovered_findings` o desperdício que você observou e que "
    "nenhuma rule_id do pacote cobre. Sem valor financeiro, sempre com "
    "evidence_ref apontando sha256 e linha de um artefato do pacote.",
    "Onde a recomendação determinística admite dois caminhos — ajustar quem "
    "escreve ou quem lê — escolha um e diga quem quebra com a escolha.",
    "Considere `constraints.rule_families_without_evidence`: nessas famílias "
    "não houve o que analisar; ausência de achado ali não é ausência de problema.",
)

#: O que o Julius já decidiu sozinho. Está aqui para o provedor não gastar
#: análise refazendo conta — e não tratar como incerto o que já é fato medido.
DETERMINISTIC = (
    "Coleta e inventário normalizado de cada ativo, com a janela e a moeda já "
    "fixadas.",
    "Quais achados existem: as regras determinísticas rodaram e o que sobrou no "
    "pacote é o que se sustenta em config declarada mais métrica medida.",
    "Quanto cada achado vale: baseline, economia esperada com faixa, fator de "
    "realização conservador e teto por processo, para o portfólio não reservar "
    "o mesmo custo duas vezes.",
    "Dificuldade, confiança, prioridade de execução e prioridade estratégica.",
    "Identidade e ciclo de vida: opportunity_id, fingerprint, status, histórico "
    "e diff entre execuções.",
    "Ownership e o grafo do processo — quem escreve, quem lê, o que depende.",
    "O que foi medido no lugar de suposto: transições por execução contadas no "
    "histórico do Step Functions, horas ociosas e invocações do CloudWatch, "
    "idle shutdown e autoscaling lidos da configuração declarada. Não reestime "
    "nenhum desses números — quando um deles falta, o pacote diz que falta.",
)


def _numbered_rules() -> str:
    return "\n".join(f"{index}. {rule}" for index, rule in enumerate(RULES, start=1))


def _estimation_methods() -> str:
    """Os métodos que o motor aceita, lidos do motor.

    Esta lista já foi escrita à mão aqui, e o resultado foi o previsível: o mapa
    de `contextual_estimation` cresceu para cinco métodos, o texto continuou com
    três, e `GLUE-CODE-SHUFFLE` e `SM-CODE-CPU-ONLY-ON-GPU` ficaram impossíveis
    de estimar — a IA não tinha como saber o nome do método, e `evaluate_proposal`
    recusa qualquer outro. Cálculo implementado e testado, desligado por uma
    frase desatualizada.

    Anunciar o par `rule_id` → método, e não só a lista de nomes, importa pela
    mesma razão: o motor recusa método que não responda àquele sinal, então uma
    lista solta obrigaria a IA a adivinhar o pareamento.
    """
    from julius.knowledge.contextual_estimation import (
        allowed_methods,
        target_parameter,
    )

    linhas = []
    for rule_id, method in sorted(allowed_methods().items()):
        alvo = target_parameter(method)
        exigencia = (
            f"`target.{alvo[0]}` — {alvo[1]}" if alvo else "`target` vazio"
        )
        linhas.append(f"   - `{rule_id}` → `{method}`, com {exigencia}")
    return "\n".join(linhas)


def _generative_eligibility() -> str:
    """Onde não há fórmula, e mesmo assim há o que dizer sobre grandeza.

    Lida da mesma fonte que o motor consulta, pelo mesmo motivo de
    `_estimation_methods`: uma lista escrita à mão aqui envelhece sem avisar. E
    quando o mapa está vazio a funcionalidade some do briefing junto — desligar é
    esvaziar o mapa, sem tocar em texto.
    """
    from julius.knowledge.generative_estimation import eligible, eligible_rule_ids

    elegiveis = eligible_rule_ids()
    if not elegiveis:
        return ""
    linhas = [
        "",
        "   Para estes, e só estes, não existe fórmula no motor e você pode devolver",
        "   `contextual_estimate` — uma faixa com raciocínio, entradas nomeadas, plano",
        "   de validação e documentação oficial. O baseline vem do pacote e não se",
        "   propõe; a faixa nunca passa dele; e nada disso entra no total oficial:",
    ]
    for rule_id in elegiveis:
        candidato = eligible(rule_id)
        assert candidato is not None  # vem de `eligible_rule_ids`
        linhas.append(
            f"   - `{rule_id}` → cobrança por `{candidato.mechanism}`; "
            f"sem fórmula porque {candidato.why_no_formula}"
        )
    return "\n".join(linhas)


def _division_of_labour(asset_types: set[str] | None = None) -> str:
    """O texto que separa o que já está resolvido do que falta responder.

    `asset_types` é o recorte: só entram as perguntas dos ativos que o pacote
    contém. `None` carrega todas, para quem monta briefing sem pacote na mão.
    """
    already = "\n".join(f"- {item}" for item in DETERMINISTIC)
    questions = render_playbooks(asset_types)
    return f"""A divisão é por grau de certeza, não por serviço.

O Julius fica com o que consegue provar: gatilho que é fato — propriedade
declarada na AWS ou métrica medida —, conclusão única e economia que sai do
próprio fato. Já está decidido e você não refaz:

{already}

Você fica com o que tem N variáveis: ler script, SQL e cadeia de dependências
para decidir se aquilo é desperdício *ali*. `collect()` sobre cem linhas é
correto e sobre cem milhões é desperdício; o mesmo AST produz os dois, nenhum
limiar resolve, e você resolve.

Suas quatro tarefas, nesta ordem:

1. Julgar cada item de `signals` contra o artefato completo — `confirmed`,
   `rejected` ou `needs_evidence`, com justificativa. Todos precisam de veredito.
   Para `confirmed`, preencha opcionalmente `recommendation` e
   `estimation_proposal`; para os demais use `null`. Cada `rule_id` abaixo aceita
   um método e só ele — propor outro é recusado pelo motor, e a proposta é o
   cenário, nunca o número:

{_estimation_methods()}
{_generative_eligibility()}
2. Enriquecer as oportunidades determinísticas: causa provável a partir da
   evidência citada, passos, dependências, conflitos e ordem de implementação.
3. Escolher o lado quando a recomendação admite dois caminhos, dizendo quem
   quebra com a escolha.
4. Registrar em `uncovered_findings` o desperdício que nenhuma regra do pacote
   cobre.

O que procurar, por tipo de ativo:
{questions}"""


def build_devin_prompt(
    context: AgentContext,
    *,
    context_file: str = "context.json",
    schema_file: str = "output-schema.json",
    result_file: str = "result.json",
) -> str:
    ativos = asset_types_in_context(context.opportunities, context.signals)
    return f"""Você está executando a Skill Julius AWS Analysis, prompt v{PROMPT_VERSION}.

Objetivo: enriquecer contextualmente as oportunidades determinísticas do Julius
e julgar os sinais que ele não consegue fechar sozinho.

{_division_of_labour(ativos)}

Regras obrigatórias:
{_numbered_rules()}
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
    """Mesmo escopo e mesmas regras, sem a mecânica de sessão de um agente."""
    ativos = asset_types_in_context(context.opportunities, context.signals)
    return f"""Análise contextual do Julius — preenchimento manual.

Conta: {context.account["id"]}
Scan: {context.scan_id}
Oportunidades no pacote: {len(context.opportunities)} de \
{context.portfolio.get("total_opportunities", len(context.opportunities))} no portfólio
Sinais a julgar: {len(context.signals)}

{_division_of_labour(ativos)}

Escreva `{result_file}` seguindo `{schema_file}`. As mesmas regras valem:

{_numbered_rules()}
"""
