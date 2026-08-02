"""As regras e o escopo que qualquer provedor de análise contextual recebe.

Duas coisas moram aqui, e são diferentes. `RULES` são guardrails — acesso
read-only, não recalcular número que o Julius já decidiu, não inventar
documentação, não vazar segredo — e cada uma tem contrapartida no validador de
resposta. `SCOPE` é o oposto: não restringe, orienta. Diz o que o Julius já
resolveu sozinho, para o provedor não repetir, e o que ele precisa procurar em
cada tipo de ativo, para não devolver texto genérico sobre um achado que já
vinha explicado.

A separação importa porque um provedor que só recebe proibição produz resposta
defensiva e vazia. Ele passa na validação e não acrescenta nada.
"""

from __future__ import annotations

from julius.analysis.context_builder import AgentContext

PROMPT_VERSION = "1.8.0"

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

#: O que o provedor precisa procurar. Perguntas, não instruções de formato: o
#: formato o schema já cobra.
SCOPE = (
    (
        "glue_job",
        (
            "O script justifica a capacidade configurada — worker type, número "
            "de workers, autoscaling?",
            "O schedule é compatível com a natureza da fonte, ou há execução "
            "que não encontra dado novo?",
            "O modo de escrita casa com o filtro de quem lê a tabela a jusante?",
            "Há reprocessamento evitável — bookmark, overwrite total, retry "
            "silencioso?",
            "O SLA tolera início adiado e capacidade não garantida, ou alguém "
            "a jusante trava esperando este job terminar na hora?",
        ),
    ),
    (
        "athena_query",
        (
            "O consumo que esta query serve ainda existe?",
            "O filtro de partição ausente é esquecimento ou requisito do caso "
            "de uso?",
            "A projeção de colunas reflete o que o consumidor usa de fato?",
        ),
    ),
    (
        "table",
        (
            "O particionamento e o layout servem ao padrão de leitura observado?",
            "Quem consome ainda depende deste formato?",
        ),
    ),
    (
        "state_machine",
        (
            "A ASL tolera semântica at-least-once, ou há Task com efeito "
            "colateral não idempotente — escrita sem chave de deduplicação, "
            "notificação, cobrança — que a reexecução do Express duplicaria?",
            "O loop de espera existe por limitação real da integração, ou por "
            "hábito onde .sync ou callback serviria?",
            "Retry e Catch cobrem falha transitória, ou escondem erro recorrente "
            "que repaga trabalho já cobrado a cada tentativa?",
        ),
    ),
    (
        "sagemaker_app",
        (
            "O trabalho executado justifica esse tipo de instância, ou é GPU "
            "parada?",
            "Isso exige Studio ativo, ou cabe num training/processing job sob "
            "demanda?",
        ),
    ),
    (
        "sagemaker_training_job",
        (
            "O script usa o acelerador que a instância cobra, ou só a biblioteca "
            "que o importa? Confirme contra o arquivo inteiro, não pelo import.",
            "O treino tolera interrupção com retomada por checkpoint? É essa "
            "resposta que libera ou barra o managed spot.",
            "As instâncias extras recebem trabalho, ou o script roda em uma só?",
            "O tempo entre o início cobrado e a primeira época é download de "
            "dado que FastFile ou Pipe evitariam?",
        ),
    ),
    (
        "redshift_cluster",
        (
            "O cluster sem conexão existe para carga sazonal, para recuperação "
            "de desastre, ou deixou de ser usado? Quem depende dele hoje?",
            "CPU baixa até no pico esconde gargalo de I/O, de memória por query "
            "ou de fila, ou é capacidade sobrando mesmo?",
        ),
    ),
    (
        "sagemaker_endpoint",
        (
            "O consumo justifica inferência em tempo real, ou Serverless/Async "
            "atende?",
            "Quem chama este endpoint hoje, e esse consumidor ainda existe?",
        ),
    ),
    (
        "s3_bucket",
        (
            "As versões não-correntes existem por exigência de retenção ou "
            "por inércia? Há prazo regulatório que impeça apagá-las?",
            "O padrão de acesso justifica reescrever este dado em classe "
            "fria, contando o custo da reescrita e o mínimo de retenção da "
            "classe alvo? Lifecycle não é opção neste ambiente.",
        ),
    ),
    (
        "cross_service",
        (
            "Quando produzir custa caro e ler desperdiça o esforço: ajustar a "
            "escrita ou a leitura? Quem quebra com a escolha?",
        ),
    ),
)


def _numbered_rules() -> str:
    return "\n".join(f"{index}. {rule}" for index, rule in enumerate(RULES, start=1))


def _division_of_labour() -> str:
    """O texto que separa o que já está resolvido do que falta responder."""
    already = "\n".join(f"- {item}" for item in DETERMINISTIC)
    questions = "\n".join(
        f"\n{asset}:\n" + "\n".join(f"- {question}" for question in items)
        for asset, items in SCOPE
    )
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
   `estimation_proposal`; para os demais use `null`. Os métodos piloto são
   `glue_interactive_capacity_reduction_v1`,
   `sagemaker_managed_spot_training_v1` e `sfn_standard_to_express_v1`.
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
    return f"""Você está executando a Skill Julius AWS Analysis, prompt v{PROMPT_VERSION}.

Objetivo: enriquecer contextualmente as oportunidades determinísticas do Julius
e julgar os sinais que ele não consegue fechar sozinho.

{_division_of_labour()}

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
    return f"""Análise contextual do Julius — preenchimento manual.

Conta: {context.account["id"]}
Scan: {context.scan_id}
Oportunidades no pacote: {len(context.opportunities)} de \
{context.portfolio.get("total_opportunities", len(context.opportunities))} no portfólio
Sinais a julgar: {len(context.signals)}

{_division_of_labour()}

Escreva `{result_file}` seguindo `{schema_file}`. As mesmas regras valem:

{_numbered_rules()}
"""
