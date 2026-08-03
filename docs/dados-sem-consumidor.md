# Dado coletado sem consumidor

Auditoria de 2 de agosto de 2026. Dos 539 campos declarados nos dataclasses de
`julius/collection/models/`, **133 não chegavam a nenhuma regra, cifra ou linha
de relatório**. Eles não somem em silêncio como um campo sem escritor — eles
consomem chamada de API, cota e tempo de coleta em toda execução, para não
influenciar nada.

`tests/test_no_dead_fields.py` passou a guardar as duas metades: campo sem
escritor (que já era verificado) e campo sem leitor a jusante (novo). A lista
`SEM_CONSUMIDOR_CONHECIDO` nomeia a dívida herdada e é uma catraca — campo novo
sem consumidor falha, e campo que ganha consumidor tem de sair da lista.

## Como ler a triagem

O teste não distingue as três situações abaixo; ele só sabe dizer que ninguém
lê o campo. A distinção é editorial e mora aqui.

### (a) Evidência a ligar — dado que é dinheiro

| campo | destino | estado |
|---|---|---|
| `S3Prefix.object_count_by_size` | a regra de arquivo pequeno usava média, que esconde distribuição bimodal — dez mil arquivos de 1 MiB numa tabela que também tem vinte de 5 GiB davam média acima do limiar e nenhum achado | **ligado** |
| `SageMakerJob.training_seconds` | a diferença para `billable_seconds` é overhead faturado (download de dado, warm pool) e é o gate de `SM-CODE-FULL-DATASET-LOAD` | **ligado** |
| `GlueJob.shuffle_read_bytes` | completa o par com `shuffle_write_bytes` no gate de `glue_shuffle_reduction_v1` | **ligado** |
| `StateMachine.timed_out_executions`, `aborted_executions` | `SFN-FAILED-TRANSITION-COST` contava só `FAILED` e subestimava a própria cifra | **ligado** |
| `S3Prefix.bytes_by_age`, `object_count_by_age` | **não** dimensionam a transição: `bytes_by_age` e `bytes_by_class` são distribuições **marginais**, e separar "bytes que compensam transitar" de "bytes que expiram antes do payback" exigiria assumir que a idade se distribui igual entre as classes. Nada mede isso, e o erro cairia direto na cifra. Entram como evidência do quanto do prefixo é antigo | **ligado** como evidência |
| `S3Prefix.bytes_by_size` | quanto a compactação precisa ler e regravar — a recomendação pedia a reescrita sem dizer o tamanho do trabalho. Só as faixas pequenas entram: objeto já no tamanho alvo não é tocado | **ligado** como evidência |
| `AthenaQuery.p50_ms`, `p95_ms` | **não** viram cifra: o Athena on-demand cobra por bytes lidos, não por tempo, e uma regra que transformasse latência em dinheiro estaria inventando o mecanismo. Servem como evidência de impacto num achado que já tem cifra própria — a pressão de fila das reservas as regras de capacidade já leem, por `query_queue_p95_ms` | **ligado** como evidência |
| `StateMachine.duration_p95_ms` | uma máquina cujo p95 passa de 300 s tem execuções que o Express mataria, mesmo com média dentro do limiar — vira motivo em `express_blockers` | ligado, mas **continua na lista**: quem o consome é a própria coleta, e o guard só conta leitor a jusante — é balde (b), não (a) |
| `GlueJob.max_execution_sec` | piso do timeout sugerido por `GLUE-TIMEOUT-EXCESSIVE`: dobrar o p95 podia propor um limite abaixo de uma execução que a janela registrou completando, e aplicá-lo cortaria um pico legítimo | **ligado** |

### (b) Intermediário legítimo da coleta

O consumidor existe e é interno. Não são dívida, e sem esta nota a próxima
auditoria os classifica como mortos de novo.

| campo | quem consome |
|---|---|
| `S3Prefix.date_partitioned` | `collection/redundant_reads.py` |
| `SageMakerJob.workload_fingerprint` | `_apply_workload_history`, que agrupa execuções do mesmo workload |
| `AthenaQuery.exact_fingerprint` | agregação de padrões em `collectors/athena/aggregate.py` |
| `CollectionHealth.started_at`, `completed_at` | janela da própria coleta, exibida via resumo |
| `StateMachine.definition_available`, `execution_history_available` | distinguem "não usa" de "não foi lido" dentro da coleta |
| `GlueJob.bytes_written_window` | `average_output_file_bytes`, a propriedade que decide se o job produz arquivo pequeno — o Glue cobra DPU-hora, não bytes escritos, então sozinho o campo não sustenta afirmação nenhuma |
| `StateMachine.duration_p95_ms` | `_apply_measured_express_blocker`, que o converte em motivo de bloqueio do Express |

### (c) Custo puro — removido

Campo sem escritor **e** sem leitor, ou medição duplicada de algo que outra
fonte já entrega:

| campo | por quê |
|---|---|
| `AthenaQuery.bytes_scanned_cutoff` | nenhuma menção fora do modelo |
| `StateMachine.distributed_map_backlog` | a regra homônima lê o agregado da conta |
| `SageMakerDomain.modeled_storage_cost` | nenhuma menção fora do modelo |
| `SageMakerJob.pipeline_execution_arn` | nenhuma menção fora do modelo |
| `StateMachine.cw_failed_executions`, `cw_timed_out_executions`, `cw_aborted_executions` | mediam pela segunda vez, sobre a mesma janela, o que `list_executions` já conta por status — três consultas CloudWatch por máquina para campos que ninguém lia |
| `StateMachine.service_integration_failures` (por máquina) | sem escritor: a métrica `ServiceIntegrationsFailed` não tem dimensão de state machine, e por isso a atribuição por máquina é impossível — a regra usa o agregado da conta e está certa |

## O que fazer com o que sobrou

A lista encolhe por decisão, não por limpeza automática. Antes de ligar um
campo, a pergunta é a mesma que vale para qualquer regra nova: **qual mecanismo
de cobrança este dado mede, e o que ele permite afirmar que hoje não se
afirma?** Campo que não responde isso é candidato ao balde (c), e remover a
chamada de API que o preenche é parte da entrega — o campo é o sintoma, a
chamada é o custo.
