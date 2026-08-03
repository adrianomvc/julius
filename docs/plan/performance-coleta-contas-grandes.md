# Plano de performance da coleta para contas AWS grandes

> O plano operacional de permissões ausentes, fallback e sequência conjunta de
> evolução está em
> [`iam-read-only-e-performance-coleta.md`](iam-read-only-e-performance-coleta.md).

> **Escopo:** acelerar a coleta read-only de uma conta Consumer com os serviços
> suportados pelo Julius, muitos recursos, muitos processos e grandes volumes em S3.
>
> **Fora de escopo:** CI, mutações na AWS, habilitação de novas fontes de
> observabilidade, mudança do motor determinístico, alteração de preços e ampliação
> implícita para outras contas ou regiões.
>
> **Estado:** em implantação. A primeira otimização interna de Athena, SageMaker,
> Step Functions e S3 foi entregue na PR #67; o scheduler e as otimizações
> seguintes estão implementados localmente e aguardam revisão/publicação.
>
> **Implantação iniciada em 2026-08-03:** Ondas 1, 2 e 3 implementadas; Onda 5
> parcialmente implementada com agregação S3 em streaming; Onda 7 parcialmente
> implementada com limite adaptativo e isolado por serviço. A matriz compartilhada
> de billing e o agrupamento concorrente de métricas CloudWatch da Onda 4 também
> foram implementados. O lote global só combina mesma janela e `ScanBy`, respeita
> o teto de 500 consultas e preserva falhas por chamador. A Onda 8 iniciou com `RunStore`,
> estado retomável, fila persistente por hash e integração opcional em `agent
> prepare/validate`; isso desacopla a entrega do pacote, mas ainda não executa um
> worker de IA durante a coleta. Checkpoints imutáveis de Glue, Athena, S3,
> SageMaker, Redshift e orquestração agora fecham durante o DAG e entram numa fila
> local sem cliente boto3; o dataset final mantém o mesmo `scan_id`. Permanecem
> Inventory existente, ampliação dos snapshots para outras fontes elegíveis,
> prioridade de caminho crítico e a execução automática do provider de IA. A
> Onda 6 começou com cache opt-in de `S3 Config`: TTL curto,
> checksum, versão e isolamento por conta/região/escopo.

---

## 1. Resultado esperado

Reduzir o tempo total e o volume de chamadas da coleta sem alterar:

- o conteúdo determinístico do `Account`;
- a ordem estável dos resultados;
- a cobertura declarada por fonte;
- os limites de custo do scan;
- a política read-only;
- o isolamento por conta;
- a região Consumer `sa-east-1`.

O ganho será medido contra o mesmo dataset e a mesma janela. Uma execução mais
rápida que omita recursos, esconda throttling ou transforme ausência em zero é
regressão, não otimização.

O plano cobre o fluxo inteiro:

```text
coleta AWS -> snapshot -> motor determinístico -> pacotes de IA -> relatório
```

A coleta não deve esperar a IA, e a entrega determinística não deve falhar porque
um provider de IA está lento ou indisponível.

## 2. Estado observado

- `collect_account()` verifica a identidade por STS e entrega as 40 fontes de
  `SOURCES` ao scheduler; fontes independentes podem rodar em paralelo e o modo
  serial continua disponível para rollback e equivalência.
- A ordem atual tem dependências reais: custo depende de inventário, S3 depende
  do escopo descoberto em Glue/Athena e alguns enriquecimentos dependem de ativos
  coletados antes.
- Cada worker produz `SourceResult` local; o coordenador aplica `account`,
  `flags`, `gaps` e saúde de forma serial. Registry de clientes e cache de
  respostas têm proteção para acesso concorrente.
- Já existe concorrência limitada dentro de pontos caros: históricos Glue,
  prefixos S3, máquinas Step Functions, detalhes SageMaker e lotes Athena.
- O cliente boto3 usa timeout, retry adaptativo e pool de conexões dimensionado;
  o scheduler também reduz e recupera a concorrência de cada serviço de forma
  isolada quando encontra throttling.
- S3 limitado lê no máximo cinco páginas por prefixo; o modo completo é opção
  explícita porque aumenta chamadas e custo.
- A telemetria estima custo de API e o orquestrador pode interromper fontes
  opcionais por `max_scan_cost_usd`.

## 3. Princípios de arquitetura

1. **Dependência explícita.** Paralelizar apenas fontes sem aresta entre elas.
2. **Resultado local.** Uma thread coleta e devolve um resultado; só o agregador
   escreve no `Account`, na saúde e nas flags.
3. **Concorrência limitada.** Há teto global e teto por serviço, não um executor
   ilimitado por coletor.
4. **Backpressure.** Paginação só produz novo trabalho quando há capacidade para
   consumi-lo.
5. **Equivalência antes de velocidade.** Serial e paralelo precisam produzir o
   mesmo JSON normalizado e a mesma saúde.
6. **Incremental honesto.** Cache ou delta sempre carregam idade, origem e regra
   de invalidação. Sem prova de atualidade, a fonte é parcial.
7. **Falha isolada.** Throttling ou permissão negada em um serviço não cancela os
   demais serviços opcionais.
8. **Sem infraestrutura nova.** O Julius pode consumir S3 Inventory, access logs
   ou métricas que já existam; nunca os habilita.
9. **Memória limitada.** Agregar páginas durante a leitura sempre que o resultado
   não precisar guardar cada item bruto.
10. **Determinismo.** Resultados concorrentes são ordenados por chaves estáveis
    antes de entrar no modelo.

## 4. Arquitetura-alvo

```text
STS + configuração + orçamento
              |
              v
      planejador de fontes
      (DAG + capacidades)
              |
              v
  executor global com limites por serviço
              |
       +------+------+----------------+
       |             |                |
   inventários    métricas       custos brutos
   independentes  em lote        consolidados
       |             |                |
       +------+------+----------------+
              |
       agregador determinístico
              |
       enriquecimentos dependentes
              |
       derivações puras + saúde
```

Cada `Source` passa a declarar:

- `depends_on`: fontes que precisam terminar antes;
- `services`: clientes AWS que utiliza;
- `writes`: campos lógicos que produz;
- `resource_class`: inventário, métrica, billing, objeto S3 ou derivação;
- `concurrency_key`: limite compartilhado, como `ce`, `s3` ou `cloudwatch`;
- `cache_policy`: sem cache, TTL, delta ou derivação pura;
- `estimated_request_class`: barata, paginada ou potencialmente massiva.

O scheduler libera uma fonte somente quando suas dependências estiverem
concluídas. O resultado é aplicado numa única thread, na ordem topológica e com
ordenação estável.

## 4.1 A IA espera a coleta hoje?

Sim, para produzir análise contextual canônica. O fluxo atual primeiro grava o
dataset completo, depois `analyze()` carrega o `Account`, executa regras e sinais,
e somente então `agent prepare` cria `context.json`. A IA não bloqueia chamadas
AWS, mas também não aproveita o tempo em que elas ainda estão rodando.

Há trabalho que pode começar antes sem violar essa fronteira:

- localizar scripts depois que Glue Jobs ou SageMaker Jobs estiverem completos;
- buscar, validar e calcular o hash dos artefatos técnicos;
- preparar índices locais e metadados de artefatos;
- executar derivações determinísticas de uma família cujas dependências já
  fecharam;
- montar um pacote imutável por domínio.

A IA não pode analisar diretamente listas ainda mutáveis nem emitir veredito
sobre uma fonte parcial como se o scan tivesse terminado.

## 4.2 Pipeline completo sem bloqueio

Criar três executores isolados:

1. **AWS I/O:** boto3, com limites globais e por serviço;
2. **CPU/local:** normalização, hashes, grafo, regras e serialização;
3. **IA:** pacotes contextuais, com fila e concorrência próprias.

Uma fila cheia de IA nunca ocupa worker boto3 nem impede nova página AWS. A
prioridade operacional é identidade, coleta e persistência do snapshot.

```text
                       +---------------------------+
                       | fila de artefatos/local   |
                       | download read-only + hash |
                       +-------------+-------------+
                                     |
STS -> DAG de coleta -> checkpoint de domínio -> regras prontas do domínio
          |                          |              |
          |                          |              v
          |                          +------> pacote IA imutável -> fila IA
          |
          v
 snapshot final -> regras globais/grafo/rateio -> relatório determinístico
                                                   |
                                      IA pronta ---+--> relatório enriquecido
                                      IA pendente -+--> anexo posterior
```

## 4.3 Checkpoint de domínio

Um checkpoint só fecha quando todas as fontes exigidas pela família terminaram,
inclusive com estado `partial` ou `unavailable`. Ele contém:

- `account_id` e `scan_id`;
- versão do schema e da Skill;
- fontes esperadas e estado de cada uma;
- janela e `data_through`;
- payload normalizado e imutável;
- hashes dos artefatos;
- hash do próprio checkpoint.

Domínios iniciais:

- `glue`: jobs, catálogo, métricas e artefatos Glue;
- `athena`: queries, workgroups, capacidade e dependências publicadas;
- `s3`: escopo, configuração, prefixos e evidência disponível;
- `sagemaker`: apps, spaces, domains, endpoints, jobs e artefatos;
- `redshift`: inventário, métricas e custo;
- `orchestration`: Step Functions, EventBridge e relações com Glue;
- `cross_service`: somente após o snapshot final e o grafo completo.

Um checkpoint parcial continua sendo útil, mas o pacote declara explicitamente a
evidência ausente. A IA responde `needs_evidence` quando ela impedir a conclusão.

## 4.4 Estados do run

Persistir um manifesto por conta e scan, com transições monotônicas:

```text
created
  -> collecting
  -> deterministic_ready
  -> deterministic_published
  -> ai_pending
  -> enriched
```

Estados laterais, sem apagar o resultado determinístico:

- `collection_partial`;
- `ai_partial`;
- `ai_failed`;
- `cancelled`;
- `superseded` quando um scan mais novo torna um pacote antigo obsoleto.

O relatório determinístico é publicável em `deterministic_published`. O estado
`enriched` adiciona contexto validado, mas nunca substitui silenciosamente campos
determinísticos.

## 4.5 Consistência da resposta da IA

Uma resposta só pode ser anexada quando coincidirem:

- conta;
- `scan_id`;
- hash do checkpoint/contexto;
- versão do prompt/Skill;
- IDs de sinais e oportunidades permitidos;
- hashes dos artefatos citados.

Resposta atrasada de scan anterior é arquivada como obsoleta, nunca aplicada ao
scan atual. A validação existente de campos determinísticos continua obrigatória.

## 4.6 Trabalho antecipado versus publicação

| Trabalho | Pode ocorrer durante coleta? | Pode ser publicado antes do final? |
|---|---:|---:|
| Criar clientes e planejar fontes | Sim | Não se aplica |
| Baixar/hash de artefato autorizado | Sim | Não |
| Normalizar resultado fechado | Sim | Não |
| Rodar regra apenas do domínio | Sim | Como preliminar interno |
| Julgar sinal com pacote fechado | Sim | Só após validação |
| Grafo e rateio cross-service | Não, exige dependências | Sim, ao fechar snapshot |
| Relatório determinístico | Após snapshot final | Sim, sem esperar IA |
| Relatório enriquecido | Conforme respostas chegam | Sim, versionado |

Não haverá texto “preliminar” misturado ao relatório oficial. Trabalho antecipado
é cache de computação; a publicação passa pelo fechamento e pela validação final.

## 4.7 Persistência e retomada

Adicionar um `RunStore` local com escritor único, aproveitando a tecnologia local
já usada pelo projeto, e diretórios imutáveis por conta/scan para payloads maiores.

O store registra:

- estados do run e das fontes;
- checkpoints fechados;
- jobs de artefato e de IA;
- tentativas, timeout e erro;
- hashes e caminhos relativos;
- qual resultado foi anexado ao relatório.

Depois de interrupção, o coordenador retoma apenas unidades concluídas e válidas.
Uma unidade em execução no momento da queda volta para `pending`; nunca se assume
que terminou.

## 4.8 Backpressure, timeout e cancelamento

- fila de IA tem tamanho máximo;
- um pacote por domínio/scan, deduplicado pelo hash;
- timeout da IA não cancela a coleta;
- retries da IA são separados de retries AWS;
- novo scan pode marcar jobs antigos como `superseded`;
- cancelamento encerra novas páginas opcionais e deixa o snapshot parcial válido;
- artefatos grandes obedecem limite de tamanho antes de entrar na fila;
- o coordenador sempre reserva capacidade para persistir saúde e finalizar o run.

## 5. Fases propostas do DAG

### Fase 0 — obrigatória e serial

- STS e validação do Account ID;
- configuração, janela e política de escopo;
- orçamento máximo de chamadas;
- criação antecipada dos clientes necessários.

Falha de identidade encerra a conta antes de qualquer outra chamada.

### Fase 1 — inventários raiz independentes

Executar em paralelo, respeitando limites por serviço:

- Cost Explorer geral;
- Glue Jobs, Catalog, Crawlers, Triggers, DataBrew e Sessions;
- Athena Queries e Provisioned Capacity;
- inventários raiz SageMaker;
- Redshift;
- Step Functions;
- EventBridge;
- CloudTrail, quando explicitamente habilitado;
- Table Touches, quando configurado.

Fontes do mesmo serviço podem compartilhar um limite menor que o global. Cost
Explorer deve começar conservador porque várias consultas concorrentes ao mesmo
endpoint tendem a disputar a mesma cota.

### Fase 2 — enriquecimentos dependentes

- CloudWatch Glue CPU/Observability depois de Glue Jobs;
- Spark Event Logs depois dos jobs relevantes;
- SageMaker Spaces depois de Apps;
- SageMaker Domains depois de Apps e Spaces;
- escopo S3 depois de Glue Catalog, Glue Jobs e Athena;
- S3 Config, Prefixes, Multipart e evidência de acesso depois do escopo S3.

### Fase 3 — cobrança e rateio

Separar consulta AWS de rateio puro:

1. coletar os buckets de cobrança necessários;
2. consolidar consultas Cost Explorer compatíveis;
3. aplicar Glue, S3, SageMaker e Redshift somente depois dos respectivos
   inventários.

Isso evita repetir consultas de billing e remove a falsa dependência entre uma
chamada de rede e o rateio em memória.

### Fase 4 — derivação serial e determinística

- última leitura de tabelas;
- leituras redundantes;
- reconciliação;
- saúde da coleta;
- estimativa de custo do scan;
- ordenação final e serialização.

## 6. Estratégia para S3 em grande escala

S3 precisa de um caminho próprio porque o custo cresce com objetos e páginas,
não apenas com buckets.

### 6.1 Reduzir o escopo antes de listar

- normalizar bucket/prefixo;
- remover prefixos duplicados;
- eliminar prefixos filhos já cobertos por um pai;
- separar prefixes de tabela, staging, resultados e event logs;
- impedir que duas fontes listem o mesmo intervalo de chaves no mesmo scan.

### 6.2 Modos de profundidade

- `bounded`: teto de páginas, cobertura declarada como limite inferior;
- `full`: listagem completa explicitamente solicitada e sujeita ao orçamento;
- `existing_inventory`: consumir S3 Inventory já existente e compatível;
- `existing_access_evidence`: consumir logs/configurações já habilitados.

O modo `existing_inventory` não configura Inventory. Se o manifesto estiver
ausente, atrasado ou incompatível com o escopo, o resultado registra a lacuna e
recua para o modo autorizado pelo operador.

### 6.3 Streaming e agregação

- processar uma página por vez;
- agregar bytes, quantidade, classe e idade sem reter todos os objetos;
- manter somente amostras/evidências necessárias ao modelo;
- usar fila limitada entre paginação e agregação;
- registrar páginas, objetos, bytes lógicos e duração por prefixo.

### 6.4 O que não será chamado de incremental

`ListObjectsV2` não oferece uma prova geral de que um bucket não mudou desde o
último scan. Sem Inventory ou outra evidência já disponível, reutilizar uma
listagem antiga como exata é proibido. Nesse caso o Julius executa a listagem
autorizada ou declara cobertura parcial.

## 7. Coleta incremental e cache

Criar um `CollectionSnapshot` por conta, com versão de schema, janela, região,
fonte, instante da coleta, parâmetros de escopo e hash do resultado.

Políticas permitidas:

| Política | Uso | Invalidação |
|---|---|---|
| Sem cache | métricas e estados voláteis | toda execução |
| TTL curto | tags e descrições de baixa volatilidade | idade, mudança de escopo ou versão |
| Delta por API | APIs com filtro temporal confiável | cursor/janela documentados |
| Snapshot imutável | artefato identificado por versão/hash | mudança da identidade |
| Derivação pura | rateio e cruzamentos locais | mudança de qualquer entrada |

Regras obrigatórias:

- chave inclui Account ID, região, fonte, escopo e versão do coletor;
- cache de uma conta nunca atende outra;
- resultado informa `fresh`, `cached`, `partial` ou `unavailable`;
- idade do cache aparece na saúde;
- mudanças de configuração invalidam entradas afetadas;
- tokens boto3 de paginação não são persistidos entre scans sem garantia
  explícita do serviço;
- dados financeiros e métricas da janela atual não usam cache vencido.

## 8. Limites de concorrência e throttling

Adicionar configuração operacional, sem mudar o perfil de análise:

- `max_in_flight_global`;
- `max_in_flight_by_service`;
- `max_queued_pages`;
- `max_memory_mb` como guarda operacional;
- `max_scan_cost_usd`, já existente;
- modo `serial` para equivalência e rollback.

Valores default serão definidos por benchmark, não por suposição. O controlador:

- começa conservador;
- reduz o limite após throttling/retries;
- recupera gradualmente quando a API estabiliza;
- não mistura a janela de controle entre serviços;
- prioriza fontes obrigatórias e dependências do caminho crítico;
- interrompe novas páginas opcionais antes de exceder orçamento.

O retry do botocore continua sendo a última defesa. Ele não substitui o limite
do scheduler.

## 9. Telemetria necessária

Por scan:

- tempo total;
- caminho crítico;
- pico de tarefas em voo;
- pico estimado de memória;
- custo estimado das chamadas;
- quantidade de fontes completas, parciais e indisponíveis.

Por fonte e serviço:

- tempo de fila e tempo de execução;
- chamadas, páginas e itens;
- cache hits/misses;
- retries e throttling;
- erros por categoria;
- workers configurados e pico efetivo;
- tempo economizado por batching quando mensurável;
- data de cobertura e idade do snapshot.

Não registrar credenciais, tokens, respostas completas nem conteúdo de objetos.

## 10. Ondas de implementação

### Onda 1 — baseline e contrato de equivalência

**Entrega**

- relatório de telemetria por fonte;
- fixture de conta pequena, média e grande;
- comparação JSON serial × otimizada;
- medição de chamadas e caminho crítico.

**Aceite**

- mesmas evidências e mesma saúde entre os modos;
- diferenças de ordem normalizadas ou eliminadas;
- baseline reproduzível localmente.

### Onda 2 — tornar o contexto seguro para concorrência

**Entrega**

- `SourceResult` local com resultado, gaps, saúde e telemetria;
- agregação em uma thread;
- registry de clientes protegido ou criado antes dos workers;
- cache e telemetria thread-safe;
- nenhuma escrita concorrente direta no `Account`.

**Aceite**

- testes com detector de corrida lógica;
- falha de uma fonte não contamina gaps de outra;
- modo serial continua disponível.

### Onda 3 — DAG e paralelismo entre serviços

**Entrega**

- `depends_on` validado para todas as fontes;
- detecção de ciclo e dependência inexistente;
- scheduler com teto global e por serviço;
- aplicação determinística dos resultados.

**Aceite**

- fontes independentes sobrepõem latência nos testes;
- dependentes nunca começam antes dos pré-requisitos;
- orçamento e políticas de escopo continuam respeitados.

### Onda 4 — consolidar Cost Explorer e CloudWatch

**Entrega**

- separar fetch de billing do rateio;
- deduplicar consultas CE equivalentes;
- agrupar lotes CloudWatch entre coletores concorrentes compatíveis;
- mapear respostas por ID estável.

**Implementado em 2026-08-03:** o cliente CloudWatch compartilhado mantém um
coordenador por scan. Chamadas concorrentes com a mesma janela e ordenação são
reunidas por até 10 ms, quebradas no limite oficial de 500 queries e devolvidas
ao coletor original. A telemetria grava requisições lógicas, queries, lotes
físicos e quantas requisições participaram de coalescência. Janelas ou `ScanBy`
diferentes nunca são misturados.

**Aceite**

- menos chamadas para o mesmo resultado;
- paginação completa;
- resposta parcial não vira zero;
- limites oficiais de cada API respeitados.

### Onda 5 — S3 massivo

**Entrega**

- trie/normalização de prefixos;
- deduplicação de intervalos;
- agregação streaming;
- filas limitadas e cancelamento por orçamento;
- adapter read-only para Inventory já existente.

**Aceite**

- memória não cresce com o total de objetos agregados;
- prefixo não é listado duas vezes;
- modo limitado mantém o teto;
- Inventory atrasado aparece como parcial.

### Onda 6 — snapshots e incremental

**Estado:** parcialmente implementada. O store versionado, telemetria de
hit/miss e a primeira política segura (`S3 Config`, TTL de 15 minutos) estão
disponíveis com `julius collect --snapshot-dir DIRETÓRIO`. Métricas, custos e
históricos continuam sempre frescos. A expansão para outras fontes depende de
separar configuração estável de estado operacional volátil em seus payloads.

**Entrega**

- armazenamento isolado por conta;
- políticas de cache por fonte;
- invalidação por versão, escopo e idade;
- reuso apenas onde a atualidade pode ser demonstrada.

**Aceite**

- scan repetido reduz chamadas elegíveis;
- dado vencido não é apresentado como atual;
- apagar snapshots apenas perde performance, nunca correção.

### Onda 7 — controle adaptativo

**Entrega**

- limites dinâmicos por serviço;
- backpressure por páginas;
- prioridade por caminho crítico;
- degradação automática para menor concorrência.

**Aceite**

- cenário com throttling termina sem tempestade de retries;
- serviços saudáveis não são reduzidos pelo throttle de outro;
- modo serial restaura o comportamento conservador.

### Onda 8 — coordenador do pipeline completo

**Entrega**

- `RunStore` e manifesto de estados;
- executores separados para AWS, CPU/local e IA;
- eventos `source_completed`, `domain_ready`, `snapshot_finalized` e
  `ai_result_validated`;
- filas limitadas, timeout, retry e retomada;
- publicação determinística independente da IA.

**Aceite**

- provider de IA bloqueado não aumenta o tempo da coleta;
- queda entre duas fontes permite retomar sem repetir unidades válidas;
- run parcial ainda produz saúde e resultado determinístico possível;
- nenhum worker de IA usa cliente ou pool boto3.

**Implementado localmente em 2026-08-03:** `collect --run-store` cria o run,
persiste checkpoints por domínio e publica o dataset determinístico sem esperar a
fila contextual. `agent next --run-store` reserva atomicamente o próximo pacote,
valida seu hash e entrega somente caminho, conta, scan e domínio — nunca sessão ou
cliente AWS. Jobs `running` podem voltar a `pending` após queda do worker.

### Onda 9 — análise contextual por checkpoint

**Entrega**

- checkpoints imutáveis por domínio;
- coleta antecipada de artefatos depois do inventário correspondente;
- pacotes de IA deduplicados por hash;
- merge final por conta, scan e versão;
- relatório com estados `ai_pending`, `ai_partial`, `ai_failed` e `enriched`.

**Aceite**

- IA começa antes do fim quando um domínio realmente fechou;
- pacote parcial declara fontes ausentes;
- resposta antiga ou com hash divergente é recusada;
- relatório determinístico sai mesmo com IA indisponível;
- resultado enriquecido não altera campo determinístico.

**Implementado parcialmente em 2026-08-03:** pacotes por domínio são canônicos,
imutáveis, isolados por conta/scan e deduplicados pelo hash. Estados `ready`,
`partial` e `unavailable` carregam a saúde das fontes. Ainda falta o executor que
chama automaticamente o provider e o merge dos resultados contextuais por domínio;
o protocolo de fila/claim já está disponível para esse worker.

### Onda 10 — homologação read-only

Somente com aprovação humana e identidade validada por STS.

Executar na mesma conta e janela:

1. baseline serial;
2. otimizado conservador;
3. otimizado balanceado;
4. otimizado com provider de IA lento e indisponível;
5. comparação de resultado, saúde, chamadas, throttling, memória e duração;
6. confirmação de que coleta e relatório determinístico não esperaram a IA.

Nenhum teste envia e-mail ou altera recurso. Como decidido para o projeto, não
haverá CI; a evidência será registrada por execução local e revisão humana.

## 11. Estratégia de testes

- equivalência serial/paralela por snapshot normalizado;
- clientes falsos com latência variável e respostas fora de ordem;
- milhares de recursos por serviço;
- milhões de objetos S3 simulados por páginas, sem materializá-los de uma vez;
- throttling intermitente e permanente;
- permissão negada em uma fonte enquanto as demais continuam;
- falha na última página;
- orçamento atingido durante paginação;
- cache válido, vencido, corrompido e de outra conta;
- ciclo no DAG e dependência ausente;
- repetição do scan para verificar determinismo;
- provider de IA lento, com timeout e fora do ar;
- resposta de IA de outro scan, conta ou versão;
- queda e retomada entre checkpoints;
- fila de IA cheia sem bloquear o executor AWS;
- pacote parcial produzindo `needs_evidence`;
- teste da allowlist read-only.

Gates locais de cada onda:

```text
pytest -q
ruff check .
mypy julius
git diff --check
```

## 12. Indicadores de sucesso

Os valores-alvo só serão fechados depois da Onda 1. O painel deve permitir
comparar, para a mesma entrada:

- duração total e por fonte;
- redução de chamadas por batching/cache;
- tempo no caminho crítico;
- retries e throttling por mil chamadas;
- pico de memória;
- custo estimado do scan;
- cobertura completa/parcial;
- divergências entre o modo serial e o otimizado.
- tempo até `deterministic_published` e até `enriched`;
- profundidade e espera da fila de IA;
- pacotes reutilizados, obsoletos e rejeitados;
- tempo de coleta com IA normal, lenta e indisponível.

Critérios que independem do baseline:

- zero divergência determinística;
- zero operação AWS fora da allowlist;
- zero mistura de snapshots entre contas;
- zero fonte parcial reportada como completa;
- nenhum crescimento de memória proporcional aos objetos S3 quando só agregados
  são necessários.

## 13. Riscos e mitigação

| Risco | Impacto | Mitigação |
|---|---|---|
| Paralelismo quebra dependência implícita | Alto | DAG declarado e validado; modo serial |
| Throttling aumenta o tempo | Alto | limites por serviço, backpressure e adaptação |
| Corrida mistura gaps/telemetria | Alto | `SourceResult` local e agregador único |
| Cache entrega dado antigo | Alto | políticas explícitas, idade visível e invalidação |
| S3 completo excede custo/memória | Alto | streaming, orçamento e modo bounded default |
| Inventory existente está atrasado | Médio | validar manifesto/data e declarar parcial |
| Ordem variável muda o relatório | Médio | ordenação por chave antes de aplicar |
| Falha de um serviço cancela a conta | Médio | isolamento por fonte; somente STS é fatal |
| Concorrência aumenta sockets | Médio | teto global e pool coerente |
| Otimização reduz cobertura silenciosamente | Alto | equivalência e saúde como gates |
| IA lenta bloqueia o scan | Alto | executores/filas separados e publicação determinística |
| IA lê estado ainda mutável | Alto | checkpoints imutáveis e hash validados |
| Resposta atrasada contamina scan novo | Alto | chave conta + scan + hash + versão |
| Retomada duplica trabalho ou resultado | Médio | jobs idempotentes e deduplicação por hash |

## 14. Rollback

- `--collection-execution serial` desliga o scheduler concorrente;
- cache pode ser ignorado sem alterar o resultado;
- Inventory pode ser desabilitado e voltar para `bounded`/`full`;
- limites adaptativos podem voltar a valores fixos;
- cada onda deve ser commitada separadamente;
- IA pode ser desligada sem afetar coleta, regras ou relatório determinístico;
- checkpoints por domínio podem ser desligados e voltar ao pacote único final;
- nenhum rollback remove os testes read-only, de equivalência ou isolamento.

## 15. Backlog priorizado

| Prioridade | Item | Onda | Ganho esperado |
|---|---|---|---|
| P0 | Baseline e equivalência serial/paralela | 1 | torna todo ganho demonstrável |
| P0 | `SourceResult` sem estado compartilhado | 2 | habilita concorrência segura |
| P0 | DAG e scheduler limitado | 3 | reduz caminho crítico entre serviços |
| P1 | Consolidar Cost Explorer | 4 | reduz chamadas repetidas e throttling |
| P1 | Lotes CloudWatch entre coletores | 4 | reduz latência e chamadas |
| P1 | Deduplicar prefixos S3 | 5 | evita listar o mesmo dado mais de uma vez |
| P1 | Agregação S3 streaming | 5 | controla memória em buckets grandes |
| P2 | Consumir Inventory existente | 5 | escala melhor para inventários enormes |
| P2 | Snapshots e cache com validade | 6 | acelera scans recorrentes |
| P2 | Delta para APIs com filtro temporal | 6 | reduz páginas em históricos extensos |
| P3 | Concorrência adaptativa | 7 | estabiliza contas próximas das cotas |
| P3 | Priorização por caminho crítico | 7 | melhora tempo total sob orçamento |
| P0 | `RunStore` e executores isolados | 8 | impede IA de bloquear coleta |
| P1 | Artefatos antecipados por domínio | 9 | aproveita o tempo restante do scan |
| P1 | Pacotes imutáveis por checkpoint | 9 | inicia IA cedo com consistência |
| P1 | Publicação determinística desacoplada | 8 | entrega resultado mesmo sem IA |
| P2 | Retomada e supersessão de scans | 8 | evita repetir trabalho após falha |
| P2 | Merge assíncrono do enriquecimento | 9 | atualiza relatório quando IA concluir |

## 16. Decisões antes da implementação

1. Onde snapshots locais serão armazenados e por quanto tempo.
2. Se o primeiro release do DAG terá apenas modo conservador ou também um modo
   balanceado configurável.
3. Qual limite de memória deve interromper novas páginas opcionais.
4. Qual conta de homologação read-only representa volume suficiente para fechar
   os defaults.
5. Qual timeout operacional deixa a IA como `pending` sem atrasar a publicação.
6. Por quanto tempo resultados contextuais obsoletos ficam retidos para auditoria.

Nenhuma dessas decisões bloqueia a Onda 1 nem a criação de `SourceResult` na
Onda 2.

## 17. Critério de conclusão do plano

O trabalho termina quando:

1. as fontes suportadas têm dependências explícitas e validadas;
2. fontes independentes executam simultaneamente sob limites por serviço;
3. o resultado concorrente é equivalente ao serial;
4. S3 massivo usa escopo deduplicado e agregação streaming;
5. scans repetidos reutilizam apenas evidência cuja validade é demonstrável;
6. telemetria mostra caminho crítico, chamadas, retries, cache, memória e custo;
7. throttling degrada concorrência sem perder cobertura silenciosamente;
8. STS continua sendo o único gate fatal de identidade;
9. toda chamada AWS permanece read-only;
10. a homologação aprovada registra ganho e equivalência na mesma conta/janela;
11. a coleta mantém duração equivalente com IA normal, lenta ou indisponível;
12. o relatório determinístico é publicado antes do enriquecido quando necessário;
13. nenhuma resposta de IA cruza conta, scan, hash ou versão;
14. interrupções podem ser retomadas a partir de checkpoints válidos.
