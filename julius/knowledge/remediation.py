"""A que ação cada regra pertence, e quanto custa medir se ela vale.

Duas regras diferentes podem ser a mesma correção. `GLUE-CODE-SHUFFLE` e
`GLUE-CODE-SINGLE-PARTITION` se resolvem reparticionando; `GLUE-CODE-REPEATED-ACTIONS`
e `GLUE-CODE-CACHE-LIFECYCLE` se resolvem cacheando o DataFrame reusado. Sem
declarar isso, o relatório mostra quatro itens onde existe uma mudança, e o leitor
conta quatro trabalhos.

O agrupamento já existia implícito. `rules/glue/code/rules.py::_has_runtime_correlation`
trata `SHUFFLE`+`SINGLE-PARTITION` como a mesma classe de evidência, e
`DRIVER-MATERIALIZATION`+`CACHE-LIFECYCLE` também — porque a métrica que confirma
uma confirma a outra. O que faltava era o nome.

**Por que o esforço mora aqui e não na regra.** A família responde as duas
perguntas com o mesmo fato: o que a correção muda decide como se mede se ela vale.
Ociosidade se mede esperando a próxima janela; shuffle se mede com benchmark A/B;
troca de modalidade exige revalidar a saída. Separar as duas coisas em dois
catálogos seria duas cópias da mesma decisão, divergindo em silêncio.

**O esforço é o de medir, nunca o de corrigir.** `Recommendation.difficulty` já
responde quanto custa aplicar a mudança. Aqui a pergunta é outra: quanto custa
descobrir se vale a pena aplicá-la. Um endpoint ocioso é caro de desligar (exige
confirmar consumidores) e barato de medir (o CloudWatch já respondeu). Confundir os
dois faria o relatório mandar investigar primeiro o que já está medido.

**A completude é cobrada por teste**, não na importação: este módulo não pode
importar `knowledge.rules` — as regras é que dependem dele — e a varredura que
encontra todo `rule_id` do fonte vive em `tests/test_remediation_catalog.py`, com a
mesma técnica de `tests/test_read_only.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from julius.findings.opportunity import Opportunity
from julius.findings.signal import Signal

#: A escala de esforço de **medição**, de 1 a 5. Cada degrau é uma coisa diferente
#: que alguém precisa fazer, não uma gradação de dificuldade sentida.
#:
#: 1. a evidência já existe, ou aparece sozinha na próxima janela — basta observar;
#: 2. exige uma leitura dirigida: listar, contar, ou perguntar ao dono;
#: 3. exige execução controlada com o mesmo volume — benchmark A/B;
#: 4. exige piloto funcional: a mudança altera a modalidade e a saída precisa ser
#:    revalidada antes de generalizar;
#: 5. exige coordenação com terceiros ou janela de manutenção.
ESFORCO_MINIMO = 1
ESFORCO_MAXIMO = 5

#: Quem confirma a hipótese. A ordem é de custo crescente para o cliente, e o
#: relatório sempre oferece o mais barato primeiro.
#:
#: - `coleta`  — o próprio Julius responde no próximo scan. O que falta é uma fonte
#:   que ele sabe ler e não leu: permissão IAM, flag de linha de comando, ou mais
#:   uma janela de observação. Quando a métrica aparece, `_has_runtime_correlation`
#:   promove o sinal a oportunidade com cifra sozinho — ninguém reprocessa nada.
#: - `analise` — a camada contextual lê o artefato inteiro e descarta ou confirma.
#:   Não mede, mas elimina falso positivo sem custo de time.
#: - `time`    — exige execução controlada (benchmark A/B, piloto) ou decisão de
#:   negócio que nenhuma API responde. É o único que consome sprint de alguém.
RESOLVEDORES = ("coleta", "analise", "time")


@dataclass(frozen=True)
class RemediationFamily:
    """Uma ação de correção, e como se descobre se ela vale nesta conta."""

    id: str
    #: Como a ação aparece para quem lê o relatório.
    label: str
    #: O que precisa ser feito para sair da hipótese. Vira a frase do próximo passo.
    measurement: str
    #: Degrau da escala acima. É o esforço de medir, nunca o de corrigir.
    effort: int
    #: Quem confirma no fim — um de `RESOLVEDORES`. É o dono **terminal**: uma
    #: família `time` pode ter um passo de `coleta` antes, e é esse passo que o
    #: relatório oferece primeiro. Sem este campo o potencial em investigação chega
    #: ao usuário como conta a pagar, quando boa parte é dívida do Julius com ele
    #: mesmo.
    resolved_by: str
    #: Por que estas regras são a mesma correção. Entrada sem esta frase não entra —
    #: é o que impede a família de virar gaveta de coisas parecidas.
    why: str


_FAMILIES: tuple[RemediationFamily, ...] = (
    RemediationFamily(
        id="capacity_sizing",
        label="Ajustar capacidade provisionada",
        measurement="rodar com capacidade menor e comparar duração e resultado",
        effort=3,
        resolved_by="time",
        why=(
            "todas descrevem capacidade maior que a demanda observada, e a correção "
            "é a mesma: reduzir o provisionamento sem mudar o que o processo entrega"
        ),
    ),
    RemediationFamily(
        id="shuffle_partitioning",
        label="Reduzir shuffle e reparticionar",
        measurement="benchmark A/B com o mesmo volume, comparando shuffle e spill",
        effort=3,
        resolved_by="time",
        why=(
            "redistribuir dado entre executores é uma decisão só; skew, spill e "
            "partição única são sintomas da mesma escolha de particionamento"
        ),
    ),
    RemediationFamily(
        id="driver_memory_cache",
        label="Parar de materializar no driver",
        measurement="pico de memória do driver antes e depois, no mesmo volume",
        effort=3,
        resolved_by="time",
        why=(
            "trazer dado para o driver e recomputar o que poderia estar em cache são "
            "o mesmo erro visto de dois ângulos, e a correção é reescrever o trecho"
        ),
    ),
    RemediationFamily(
        id="row_level_processing",
        label="Substituir processamento linha a linha",
        measurement="benchmark A/B trocando a operação por equivalente nativo",
        effort=3,
        resolved_by="time",
        why=(
            "UDF Python e I/O externo por linha impedem a mesma otimização do motor "
            "distribuído, e a saída é trocar a operação por uma vetorizada"
        ),
    ),
    RemediationFamily(
        id="read_pruning",
        label="Ler menos dado",
        measurement="bytes lidos antes e depois, numa execução de controle",
        effort=2,
        resolved_by="time",
        why=(
            "filtro na leitura, partição e projeção de coluna atacam a mesma conta: "
            "o volume que entra no processamento"
        ),
    ),
    RemediationFamily(
        id="output_layout",
        label="Corrigir o layout de saída",
        measurement="contar e medir os arquivos escritos numa execução",
        effort=2,
        resolved_by="coleta",
        why=(
            "tamanho de arquivo, formato e compressão são decisões da mesma escrita, "
            "e quem as corrige mexe no mesmo ponto do código"
        ),
    ),
    RemediationFamily(
        id="incremental_state",
        label="Processar só o que mudou",
        measurement="comparar bytes lidos com e sem estado incremental",
        effort=3,
        resolved_by="time",
        why=(
            "bookmark desligado, contexto ausente e commit faltando são três formas "
            "de o mesmo mecanismo incremental não funcionar"
        ),
    ),
    RemediationFamily(
        id="runtime_modality",
        label="Trocar a modalidade de execução",
        measurement="piloto funcional com a modalidade nova e saída revalidada",
        effort=4,
        resolved_by="time",
        why=(
            "mudar runtime, tipo de instância, Flex, Spot ou Express altera onde o "
            "processo roda; todas exigem revalidar a saída antes de generalizar"
        ),
    ),
    RemediationFamily(
        id="idle_capacity",
        label="Desligar o que não é usado",
        measurement="observar mais uma janela e confirmar consumidores e owner",
        effort=1,
        resolved_by="coleta",
        why=(
            "recurso ligado sem demanda observada; a correção é a mesma — desligar "
            "ou arquivar — e o que falta é sempre confirmar que ninguém depende dele"
        ),
    ),
    RemediationFamily(
        id="schedule_frequency",
        label="Ajustar a frequência de execução",
        measurement="confirmar com o dono a cadência que a fonte exige",
        effort=2,
        resolved_by="time",
        why=(
            "rodar mais vezes que a fonte muda é o mesmo desperdício, venha de cron, "
            "trigger, crawler ou execução avulsa"
        ),
    ),
    RemediationFamily(
        id="failure_waste",
        label="Parar de pagar por execução que falha",
        measurement="somar o custo das execuções que falharam na janela",
        effort=1,
        resolved_by="coleta",
        why=(
            "falha, timeout, retry e reprocessamento cobram capacidade sem entregar "
            "resultado; a correção é sempre corrigir a causa da falha"
        ),
    ),
    RemediationFamily(
        id="orchestration_waste",
        label="Eliminar transição de estado desnecessária",
        measurement="contar transições por execução antes e depois",
        effort=2,
        resolved_by="analise",
        why=(
            "polling manual, retry sem teto e fanout sem limite cobram transição do "
            "Step Functions pelo mesmo mecanismo, e a correção é a definição ASL"
        ),
    ),
    RemediationFamily(
        id="silent_failure",
        label="Deixar de engolir o erro",
        measurement="revisar o log da janela atrás do erro suprimido",
        effort=2,
        resolved_by="analise",
        why=(
            "exceção capturada sem tratamento esconde o desperdício em vez de causá-lo; "
            "a correção é a mesma — propagar o erro — e ela precede qualquer estimativa"
        ),
    ),
    RemediationFamily(
        id="training_convergence",
        label="Parar de treinar além da convergência",
        measurement="curva de validação de um treino completo",
        effort=3,
        resolved_by="time",
        why=(
            "épocas fixas cobram instância depois de o modelo parar de melhorar; a "
            "correção é o critério de parada"
        ),
    ),
    RemediationFamily(
        id="table_format",
        label="Migrar o formato da tabela",
        measurement="piloto de reescrita com os consumidores revalidados",
        effort=4,
        resolved_by="time",
        why=(
            "trocar o formato da tabela reescreve o dado e muda como todo "
            "consumidor a lê; é decisão de arquitetura do produto de dados, e não "
            "cabe com compactar arquivo pequeno, que mexe só em quem escreve"
        ),
    ),
    RemediationFamily(
        id="storage_class",
        label="Mover objeto de classe de armazenamento",
        measurement="evidência de leitura do prefixo e composição por classe",
        effort=2,
        resolved_by="coleta",
        why=(
            "transição e reescrita de dado frio mudam a mesma linha da fatura, e as "
            "duas dependem de provar que o dado não é lido"
        ),
    ),
    RemediationFamily(
        id="lifecycle_cleanup",
        label="Remover o que sobrou",
        measurement="listar o que ficou e confirmar que nada depende dele",
        effort=1,
        resolved_by="coleta",
        why=(
            "resultado antigo, log, staging, multipart e versão não corrente ocupam "
            "armazenamento sem consumidor; a correção é a mesma remoção"
        ),
    ),
    RemediationFamily(
        id="data_demand",
        label="Parar de produzir dado sem consumidor",
        measurement="confirmar consumidores da tabela com o dono do produto",
        effort=2,
        resolved_by="time",
        why=(
            "tabela sem leitura e pipeline que a produz são as duas pontas do mesmo "
            "desperdício; agir numa sem a outra não reduz a conta"
        ),
    ),
    RemediationFamily(
        id="result_reuse",
        label="Reaproveitar resultado já computado",
        measurement="repetições exatas elegíveis na janela",
        effort=1,
        resolved_by="coleta",
        why="a mesma consulta recomputada cobra varredura que o reuso evita",
    ),
    RemediationFamily(
        id="observability_gap",
        label="Habilitar a métrica que falta",
        measurement="nenhuma: a lacuna é de configuração, não de medição",
        effort=1,
        resolved_by="coleta",
        why=(
            "sem a métrica nenhuma outra família consegue medir; é pré-requisito de "
            "diagnóstico, e por isso não recebe economia própria"
        ),
    ),
    RemediationFamily(
        id="commitment_purchase",
        label="Rever compromisso de compra",
        measurement="cobertura e utilização do compromisso na janela",
        effort=2,
        resolved_by="time",
        why="desconto por compromisso é decisão financeira sobre consumo já medido",
    ),
    RemediationFamily(
        id="advisor_backlog",
        label="Aplicar recomendação do próprio serviço",
        measurement="nenhuma: a AWS já publicou o número",
        effort=1,
        resolved_by="coleta",
        why="a recomendação vem pronta do serviço e só falta decidir aplicá-la",
    ),
    RemediationFamily(
        id="inventory_integrity",
        label="Corrigir a atribuição do custo",
        measurement="reconciliar a cobrança com o inventário coletado",
        effort=1,
        resolved_by="coleta",
        why=(
            "mede a qualidade da coleta, não desperdício na conta; fica fora do "
            "portfólio por decisão de produto e aparece em seção própria"
        ),
    ),
)

FAMILIES: dict[str, RemediationFamily] = {item.id: item for item in _FAMILIES}


#: `rule_id` → família. A ordem segue a do catálogo acima para a revisão ser
#: possível: quem lê confere um bloco por vez contra a frase que o justifica.
CATALOG: dict[str, str] = {
    # capacity_sizing
    "GLUE-OVERPROVISIONED": "capacity_sizing",
    "GLUE-WORKER-TYPE-OVERSIZED": "capacity_sizing",
    "GLUE-AUTOSCALING": "capacity_sizing",
    "GLUE-EXECUTOR-CAPACITY-GAP": "capacity_sizing",
    "GLUE-IS-CAPACITY-REVIEW": "capacity_sizing",
    "REDSHIFT-OVERSIZED": "capacity_sizing",
    "REDSHIFT-RESIZE-TARGET": "capacity_sizing",
    "REDSHIFT-SERVERLESS-MAX-CAPACITY-MISSING": "capacity_sizing",
    "REDSHIFT-SERVERLESS-RPU-HOURS-LIMIT-MISSING": "capacity_sizing",
    "SM-ENDPOINT-RIGHTSIZE": "capacity_sizing",
    "SM-ENDPOINT-RECOMMENDER-SAVING": "capacity_sizing",
    "SM-JOB-RIGHTSIZE": "capacity_sizing",
    "SM-JOB-RIGHTSIZE-CANDIDATE": "capacity_sizing",
    "SM-APP-INSTANCE-FIT": "capacity_sizing",
    "SM-ASYNC-MIN-CAPACITY-IDLE": "capacity_sizing",
    "SM-SERVERLESS-PC-IDLE": "capacity_sizing",
    "SM-FEATURE-STORE-PROVISIONED-IDLE": "capacity_sizing",
    "ATHENA-CAPACITY-LOW-UTILIZATION": "capacity_sizing",
    "ATHENA-CAPACITY-UNASSIGNED": "capacity_sizing",
    "ATHENA-CAPACITY-CONCENTRATED-DEMAND": "capacity_sizing",
    "ATHENA-CAPACITY-QUEUE-PRESSURE": "capacity_sizing",
    # shuffle_partitioning
    "GLUE-CODE-SHUFFLE": "shuffle_partitioning",
    "GLUE-CODE-SHUFFLE-PARTITIONS": "shuffle_partitioning",
    "GLUE-CODE-SINGLE-PARTITION": "shuffle_partitioning",
    "GLUE-CODE-JDBC-SINGLE-READER": "shuffle_partitioning",
    "GLUE-TASK-SKEW": "shuffle_partitioning",
    "GLUE-SHUFFLE-SPILL": "shuffle_partitioning",
    # driver_memory_cache
    "GLUE-CODE-DRIVER-MATERIALIZATION": "driver_memory_cache",
    "GLUE-CODE-CACHE-LIFECYCLE": "driver_memory_cache",
    "GLUE-CODE-REPEATED-ACTIONS": "driver_memory_cache",
    "GLUE-CODE-ITERATIVE-PLAN": "driver_memory_cache",
    "SM-CODE-FULL-DATASET-LOAD": "driver_memory_cache",
    # row_level_processing
    "GLUE-CODE-PYTHON-UDF": "row_level_processing",
    "GLUE-CODE-ROW-EXTERNAL-IO": "row_level_processing",
    "SM-CODE-ROW-EXTERNAL-IO": "row_level_processing",
    # read_pruning
    "GLUE-CODE-PUSHDOWN": "read_pruning",
    "GLUE-CODE-S3-FULL-SCAN": "read_pruning",
    "ATHENA-NO-PARTITION-FILTER": "read_pruning",
    "ATHENA-FULL-TABLE-SCAN": "read_pruning",
    "ATHENA-SELECT-STAR-WIDE": "read_pruning",
    "ATHENA-EXCESSIVE-SCAN": "read_pruning",
    "ATHENA-TABLE-NOT-PARTITIONED": "read_pruning",
    "ATHENA-PARTITION-PROJECTION": "read_pruning",
    "ATHENA-BYTES-SCANNED-CUTOFF": "read_pruning",
    # Formato e compressão entram aqui, e não em `output_layout`, porque no Athena
    # todas estas regras se prendem ao mesmo ativo — `athena_query` — e movem a
    # mesma alavanca: os bytes que a query varre. Em Glue e S3 o layout de saída é
    # outro ativo e outra alavanca, e por isso lá a família se separa.
    #
    # Separá-las produziria duas ações sobre o mesmo padrão de query, e o produto
    # já decidiu o contrário: o motor entrega uma ação por padrão, e escolher entre
    # ajustar quem escreve ou quem produz a tabela é julgamento da análise
    # contextual. `tests/test_athena_monthly.py` cobra essa decisão.
    "ATHENA-UNCOMPRESSED-ROW-FORMAT": "read_pruning",
    "ATHENA-COLUMNAR-COMPRESSION": "read_pruning",
    "ATHENA-SMALL-FILES": "read_pruning",
    # output_layout
    "GLUE-CODE-SMALL-FILES": "output_layout",
    "GLUE-SMALL-FILES-OUTPUT": "output_layout",
    "GLUE-CODE-FULL-OVERWRITE": "output_layout",
    "S3-SMALL-FILES": "output_layout",
    # incremental_state
    "GLUE-BOOKMARK-OFF": "incremental_state",
    "GLUE-CODE-BOOKMARK-CONTEXT": "incremental_state",
    "GLUE-CODE-BOOKMARK-COMMIT": "incremental_state",
    # runtime_modality
    "GLUE-SPARK-TO-PYTHON-SHELL": "runtime_modality",
    # Sessão que roda como job: a mudança é onde o trabalho executa, e o piloto
    # precisa revalidar a saída — mesma alavanca de trocar runtime ou modalidade.
    "GLUE-IS-TO-JOB": "runtime_modality",
    "GLUE-VERSION-OLD": "runtime_modality",
    "GLUE-VERSION-REVIEW": "runtime_modality",
    "GLUE-FLEX-CANDIDATE": "runtime_modality",
    "GLUE-FLEX-TOLERANCE": "runtime_modality",
    "SFN-STANDARD-TO-EXPRESS": "runtime_modality",
    "SM-TRAINING-SPOT-CANDIDATE": "runtime_modality",
    "SM-CODE-NO-CHECKPOINT": "runtime_modality",
    "SM-CODE-CPU-ONLY-ON-GPU": "runtime_modality",
    "SM-CODE-SINGLE-DEVICE-MULTI-INSTANCE": "runtime_modality",
    "SM-LEGACY-GPU-FAMILY": "runtime_modality",
    "SM-ENDPOINT-MODE-FIT": "runtime_modality",
    # idle_capacity
    "GLUE-IS-IDLE-TIMEOUT": "idle_capacity",
    "GLUE-JOB-INACTIVE-90D": "idle_capacity",
    "GLUE-JOB-ABANDONED": "idle_capacity",
    "GLUE-STREAMING-NO-INPUT": "idle_capacity",
    "GLUE-NO-INPUT-WASTE": "idle_capacity",
    "REDSHIFT-IDLE-CLUSTER": "idle_capacity",
    "REDSHIFT-IDLE-JUSTIFICATION": "idle_capacity",
    "SM-APP-IDLE": "idle_capacity",
    "SM-APP-IDLE-CANDIDATE": "idle_capacity",
    "SM-ENDPOINT-ZERO-TRAFFIC": "idle_capacity",
    "SM-ENDPOINT-ZERO-TRAFFIC-CANDIDATE": "idle_capacity",
    "SM-NOTEBOOK-RUNNING": "idle_capacity",
    "SM-SPACE-STORAGE-IDLE": "idle_capacity",
    "SM-DOMAIN-EFS-IDLE": "idle_capacity",
    "SM-DOMAIN-EFS-STORAGE-IDLE": "idle_capacity",
    "SM-WARM-POOL-UNUSED": "idle_capacity",
    "SM-FEATURE-STORE-ONLINE-UNUSED": "idle_capacity",
    # schedule_frequency
    "GLUE-FREQUENCY-REVIEW": "schedule_frequency",
    "GLUE-SCHEDULE-RUN-MISMATCH": "schedule_frequency",
    "GLUE-CRAWLER-SCHEDULE-DISABLED": "schedule_frequency",
    "GLUE-CRAWLER-NO-CHANGES": "schedule_frequency",
    "GLUE-CRAWLER-FULL-RECRAWL": "schedule_frequency",
    "DATABREW-SCHEDULE-RUN-MISMATCH": "schedule_frequency",
    "PROCESS-NON-RECURRING-COST": "schedule_frequency",
    # failure_waste
    "GLUE-FAILING-JOB": "failure_waste",
    "GLUE-TIMEOUT-EXCESSIVE": "failure_waste",
    "GLUE-OVERLAPPING-RUNS": "failure_waste",
    "GLUE-CRAWLER-FAILING": "failure_waste",
    "DATABREW-FAILING-JOB": "failure_waste",
    "ATHENA-RECURRENT-FAILURES": "failure_waste",
    "SFN-FAILED-TRANSITION-COST": "failure_waste",
    "SFN-RETRY-WASTE": "failure_waste",
    "SFN-RETRY-MASKING": "failure_waste",
    "SFN-REDRIVE-REPROCESSING": "failure_waste",
    "SFN-SERVICE-INTEGRATION-FAILURE": "failure_waste",
    "SFN-SERVICE-INTEGRATION-TIMEOUT": "failure_waste",
    "SFN-STUCK-OPEN-EXECUTIONS": "failure_waste",
    "SFN-EXECUTION-THROTTLING": "failure_waste",
    "SFN-DISTRIBUTED-MAP-BACKLOG": "failure_waste",
    "SM-ENDPOINT-HEALTH": "failure_waste",
    "SM-PIPELINE-RETRY-PATTERN": "failure_waste",
    "SM-MODEL-MONITOR-HEALTH": "failure_waste",
    # orchestration_waste
    "SFN-POLLING-LOOP": "orchestration_waste",
    "SFN-ASL-MANUAL-POLLING": "orchestration_waste",
    "SFN-ASL-RETRY-UNBOUNDED": "orchestration_waste",
    "SFN-ASL-UNBOUNDED-FANOUT": "orchestration_waste",
    # silent_failure
    "GLUE-CODE-SWALLOWED-EXCEPTION": "silent_failure",
    "SM-CODE-SWALLOWED-EXCEPTION": "silent_failure",
    "SFN-ASL-CATCH-SWALLOW": "silent_failure",
    # training_convergence
    "SM-CODE-FIXED-EPOCHS": "training_convergence",
    # table_format
    "GLUE-TABLE-FORMAT-REVIEW": "table_format",
    # storage_class
    "S3-STORAGE-CLASS-TRANSITION": "storage_class",
    "S3-COLD-DATA-REWRITE": "storage_class",
    # lifecycle_cleanup
    "S3-ATHENA-RESULTS-STALE": "lifecycle_cleanup",
    "S3-SPARK-LOGS-STALE": "lifecycle_cleanup",
    "S3-JOB-STAGING-LEFTOVER": "lifecycle_cleanup",
    "S3-INCOMPLETE-MULTIPART": "lifecycle_cleanup",
    "S3-NONCURRENT-VERSIONS": "lifecycle_cleanup",
    "SM-FEATURE-STORE-TTL-GAP": "lifecycle_cleanup",
    # data_demand
    "DATA-UNUSED-OUTPUT": "data_demand",
    "DATA-LOW-USE-SINGLE-CONSUMER": "data_demand",
    "XSVC-WASTED-PRODUCTION": "data_demand",
    # result_reuse
    "ATHENA-RESULT-REUSE": "result_reuse",
    # observability_gap
    "GLUE-OBSERVABILITY-OFF": "observability_gap",
    "GLUE-CONTINUOUS-LOGGING-OFF": "observability_gap",
    # commitment_purchase
    "SM-SAVINGS-PLAN-FINOPS": "commitment_purchase",
    # advisor_backlog
    "REDSHIFT-ADVISOR-UNAPPLIED": "advisor_backlog",
    # inventory_integrity
    "GLUE-UNATTRIBUTED-COST": "inventory_integrity",
    "ATHENA-CAPACITY-COST-UNAVAILABLE": "inventory_integrity",
    "EFFICIENCY-REGRESSION": "inventory_integrity",
}


def family_for(rule_id: str) -> RemediationFamily | None:
    """A família de uma regra, ou `None` quando ela ainda não foi classificada.

    `None` e uma família genérica dizem coisas diferentes: a segunda agruparia o
    achado novo com achados que não são a mesma correção, e ninguém notaria. O
    teste de completude é o que impede este `None` de sobreviver a um commit.
    """
    family_id = CATALOG.get(rule_id)
    return FAMILIES.get(family_id) if family_id else None


def measurement_effort(rule_id: str) -> int:
    """Quanto custa descobrir se esta regra vale nesta conta.

    Regra sem família recebe o degrau mais alto entre os observados, e não o mais
    baixo: o desconhecido não pode competir por atenção com o que já foi medido.
    """
    family = family_for(rule_id)
    return family.effort if family is not None else ESFORCO_MAXIMO


#: Que fontes de coleta, se voltarem completas, respondem sozinhas a medição de um
#: ativo. É a chave da frase mais útil do relatório: boa parte do "potencial em
#: investigação" não é conta a pagar pelo time — é dívida do Julius com ele mesmo,
#: e se resolve com uma permissão IAM ou uma flag.
#:
#: O mapa é por **tipo de ativo**, e não por família, porque a fonte é do serviço:
#: `capacity_sizing` existe em Glue, Redshift, SageMaker e Athena, e cada um se
#: mede numa fonte diferente. `tests/test_pending_measurements.py` confere que todo
#: nome aqui existe em `collection/sources.py`.
UNBLOCKING_SOURCES: dict[str, tuple[str, ...]] = {
    "glue_job": (
        "Glue Jobs",
        "CloudWatch Glue CPU",
        "CloudWatch Glue Observability",
        "Spark Event Logs",
    ),
    "glue_session": ("Glue Interactive Sessions",),
    "glue_crawler": ("Glue Crawlers",),
    "databrew_job": ("Glue DataBrew",),
    "athena_query": ("Athena Queries",),
    "athena_workgroup": ("Athena Provisioned Capacity",),
    "state_machine": ("Step Functions", "EventBridge Schedules"),
    "redshift_cluster": ("Amazon Redshift", "Redshift Cost Explorer"),
    "table": ("Glue Catalog", "Table Touches"),
}

#: Prefixos de tipo de ativo, para os que se subdividem. `sagemaker_app`,
#: `sagemaker_endpoint` e os demais compartilham as mesmas fontes.
_UNBLOCKING_BY_PREFIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sagemaker_", ("SageMaker Studio", "SageMaker Endpoints", "SageMaker Jobs")),
    ("s3_", ("Amazon S3", "S3 Prefixes", "S3 Access Evidence")),
)


def unblocking_sources(asset_type: str) -> tuple[str, ...]:
    """As fontes cuja lacuna explica a medição que falta neste ativo."""
    exato = UNBLOCKING_SOURCES.get(asset_type)
    if exato is not None:
        return exato
    for prefixo, fontes in _UNBLOCKING_BY_PREFIX:
        if asset_type.startswith(prefixo):
            return fontes
    return ()


def resolved_by(rule_id: str) -> str:
    """Quem confirma esta hipótese no fim.

    Regra sem família cai em `time`, o mais caro, pelo mesmo motivo do esforço: o
    desconhecido não pode ser oferecido como se fosse barato.
    """
    family = family_for(rule_id)
    return family.resolved_by if family is not None else "time"


def rule_ids_in(family_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(rule for rule, item in CATALOG.items() if item == family_id)
    )


def classify_opportunities(opportunities: Iterable[Opportunity]) -> None:
    """Carimba a família em cada achado, no lugar. Mesma razão de `classify`.

    `Opportunity` é mutável e `Signal` não, então um devolve lista e o outro
    escreve — a assimetria é dos tipos, não da intenção.
    """
    for opportunity in opportunities:
        opportunity.remediation_family = CATALOG.get(opportunity.rule_id, "")


def classify(signals: Iterable[Signal]) -> list[Signal]:
    """Carimba família e esforço de medição em cada sinal.

    Roda num lugar só, e não em cada regra que emite sinal, por duas razões. A
    primeira é a seta entre camadas: `findings.Signal` não pode consultar este
    catálogo, porque `findings` não enxerga `knowledge`. A segunda é que espalhar a
    consulta por vinte e nove pontos de construção faria a próxima regra nascer sem
    classificação e ninguém perceber — que é exatamente o que o catálogo existe
    para impedir.
    """
    return [
        replace(
            signal,
            remediation_family=CATALOG.get(signal.rule_id, ""),
            measurement_effort=measurement_effort(signal.rule_id),
        )
        for signal in signals
    ]


__all__ = [
    "CATALOG",
    "ESFORCO_MAXIMO",
    "ESFORCO_MINIMO",
    "FAMILIES",
    "RESOLVEDORES",
    "RemediationFamily",
    "classify",
    "classify_opportunities",
    "family_for",
    "measurement_effort",
    "resolved_by",
    "rule_ids_in",
]
