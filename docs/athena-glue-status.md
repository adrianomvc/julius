# Athena e Glue — estado, contrato de custo e pendências

Documento único de estado dos dois serviços que concentram a análise do Julius.
Complementa o [runbook de homologação Athena](athena-operational-validation.md)
e o [detalhamento técnico do Glue](glue-analysis.md).

## Contrato de janela

- **Dois períodos, nomeados, que nunca se somam.** A *janela de análise* tem N
  dias UTC **completos** (padrão 30) e é o período de tudo que é comparado:
  comportamento, custo, rateio, baseline de oportunidade — idêntica em Glue e
  Athena. O *painel de fatura* é o mês-calendário até o último dia fechado, e
  existe só para reconciliar com o que a AWS emite.
- O teto prático da janela é o histórico de execuções do Athena, que a AWS
  retém por cerca de 45 dias.
- **A janela é construída uma vez, em UTC** (`aws/window.py`), e passada a
  todos os coletores. Nenhum coletor chama `date.today()`: era essa mistura de
  fuso local com UTC que deslocava o corte de consumo em relação ao corte da
  cobrança num scan noturno de fim de mês.
- **Nada é extrapolado.** Trinta dias completos já são um período realizado; a
  projeção que multiplicava um mês parcial por `dias_do_mês / dia_observado`
  saiu do caminho de análise. Onde a projeção de fechamento continua sendo
  exibida, ela exige um mínimo de dias observados.
- **30 dias não são um mês.** O mês médio tem 30,44 dias. Um número por mês é a
  medição da janela multiplicada por um fator explícito e único
  (`DAYS_PER_MONTH / window_days`), nunca a janela renomeada.
- **Free tier do Data Catalog é por mês-calendário** e a janela móvel cruza essa
  fronteira. Afeta apenas o bucket `catalog`, que já não é rateado.
- Um dataset do esquema anterior mede mês-corrente e **é recusado na ingestão**
  (`unsupported_dataset_version`): renomear o campo não converte o número.

## Contrato de custo

- **USD é a única moeda, e não há conversão.** Cost Explorer, Cost and Usage
  Report, Budgets e a Price List API reportam em USD independentemente da moeda
  de pagamento configurada na conta. A preferência de moeda do Billing afeta
  somente a fatura final: a conversão acontece do lado da AWS, na emissão, com
  o câmbio do dia.
- Por isso o Julius **não tem tabela de câmbio**. Uma resposta fora de USD é
  anomalia, não caso de conversão: o valor não é reinterpretado nem convertido
  por taxa própria. A fonte registra a lacuna e a categoria
  `unsupported_currency` na saúde da coleta, e o número não aparece no
  relatório.
- Valor zerado é aceito em qualquer unidade — zero não tem moeda, e a AWS
  preenche a unidade de formas diferentes em grupos sem cobrança.
- Um dataset exportado sem `currency` declarada é anterior a esse contrato e é
  recusado na ingestão, em vez de ser reinterpretado como dólar.
- Custo de fatura nunca é apresentado como custo por query ou por job sem que a
  qualidade seja `reconciled`.
- Qualidade de custo tem três estados em ambos os serviços: `reconciled`,
  `partial`, `unavailable`.

Referências:
[preferências de pagamento](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/manage-payment-method.html)
e [moedas suportadas](https://repost.aws/knowledge-center/supported-aws-currencies).

## Granularidade — o que "custo real" significa

O Cost Explorer não expõe dimensão de recurso para Glue nem para Athena. A
cobrança real chega agregada por `USAGE_TYPE`. O custo por job e por padrão de
query é **rateio dessa cobrança** pelo consumo medido (DPU-hora, bytes
faturáveis) — nunca fatura por recurso.

Custo real por execução exigiria uma destas dependências externas, ambas fora
da coleta read-only atual:

- **tags de alocação de custo** (`GroupBy TAG`): exige ativação da tag no
  console de Billing, ação administrativa, e recursos taggeados;
- **CUR / Data Exports** (`line_item_resource_id`): custo por execução, exige
  export configurado e leitura via Athena/S3.

## Fronteira entre o determinístico e a IA

A divisão entre o código Python e a análise contextual não é por serviço. É
pelo quanto a evidência fecha.

**O Python fica com o que ele consegue provar.** Uma regra pertence aqui quando
as três condições valem ao mesmo tempo:

1. **o gatilho é fato** — propriedade declarada na AWS ou métrica medida; não
   padrão sintático, não distância de um default;
2. **a conclusão é única** — duas pessoas competentes olhando o mesmo dado
   chegam à mesma ação;
3. **a economia sai do fato** — sem supor intenção, volume ou necessidade de
   negócio.

Repetições exatas contadas no histórico do workgroup com bytes faturáveis
medidos fecham (`ATHENA-RESULT-REUSE`). Timeout declarado contra p95 medido
fecha. Endpoint 24/7 com zero invocações fecha. O Julius afirma, precifica e
ordena esses casos.

**A análise contextual fica com o que tem N variáveis.** Ler o SQL, o script ou
a cadeia de dependências para decidir se aquilo é desperdício *ali* não é coisa
de limiar. `collect()` sobre cem linhas é correto e sobre cem milhões é
desperdício, e o mesmo AST produz os dois. Se o filtro de partição ausente é
esquecimento ou requisito do caso de uso, só o consumo a jusante diz. Se migrar
o runtime é seguro depende de bibliotecas que só o script revela. Esses casos
chegam à IA como **sinais** — a observação, o hash do artefato, as linhas e a
evidência que falta — e voltam confirmados, descartados ou pedindo evidência.

Nenhuma das camadas atravessa a outra. A IA nunca calcula nem altera economia,
dificuldade, confiança ou prioridade; o Python nunca afirma desperdício a partir
de padrão que não consegue corroborar.

**Quando a regra está certa e falta dado, o caminho é coleta — não IA.** Vários
itens de *Pendente* abaixo são exatamente isso: o gatilho é medido, a ação é
única, e a economia sai zerada ou com confiança capada porque a métrica que a
quantificaria não foi coletada. Pedir à IA que estime esse número seria pedir
que ela adivinhe o que a AWS publica.

## Athena

### Feito

- coleta read-only por workgroup, com paginação completa, sobre a janela de
  análise compartilhada (`aws/athena_collector.py`);
- enriquecimento por Glue Catalog e S3: particionamento, formato, compressão,
  arquivos pequenos, tabelas wide, candidatos a partition projection;
- parsing AST com `sqlglot`, fingerprint estrutural e exato, elegibilidade de
  result reuse com janela conservadora de 60 minutos;
- atribuição de ator por CloudTrail e Identity Center, sem persistir SQL
  original, `QueryExecutionId`, IP ou bloco `userIdentity`;
- reconciliação API × CloudWatch `ProcessedBytes` com banda de 95%–105%;
- custo líquido alocado do Cost Explorer (`NetUnblendedCost`, com fallback
  explícito para `UnblendedCost`) e gates de `cost_quality`;
- 11 regras `ATHENA-*` com faixas de recuperação versionadas
  (`ATHENA_RECOVERY_RATES`), separando economia medida de estimativa modelada.
  Compressão e partition projection são exceção deliberada: a config é fato
  declarado pela tabela, mas quanto ela rende depende do padrão de acesso, então
  o achado permanece e a faixa modelada sai da soma do portfólio
  (`is_strategic`);
- integração completa em `report.html`, `report.json`, `email.html` e
  `email.txt`; cobertura em `tests/test_athena_monthly.py`.

### Pendente

- **homologação read-only em conta real** — depende de identidade corporativa;
  procedimento e critérios já descritos em
  [athena-operational-validation.md](athena-operational-validation.md);
- **capacidade provisionada**: `detectors/athena.py` continua sem modelar
  `modality != on_demand` — a cobrança é por DPU reservada, não por bytes. As
  queries ignoradas agora são contadas e nomeadas em `AthenaCoverage.gaps`, em
  vez de sumirem em silêncio, mas contas com capacity reservation seguem sem
  regra própria;
- **controles de workgroup**: limite de dados por query, result reuse habilitado
  no próprio workgroup e migração para engine v3 ainda não viram regra. São
  candidatos determinísticos claros — config declarada, decisão fechada — e o
  `BytesScannedCutoffPerQuery` é o guardrail mais barato que o Athena oferece.
  O campo `bytes_scanned_cutoff` existe em `collection/models/athena.py`, mas
  **nenhum coletor o preenche**: falta chamar `GetWorkGroup` antes de escrever a
  regra;
- **ciclo de vida do bucket de resultados**: o custo S3 dos resultados de query
  não é avaliado;
- **queries federadas**: o custo Lambda associado não entra na análise.

## Glue

### Feito

- inventário de jobs independente do modo de autoria, com DPU-hora **medida**
  (`DPUSeconds`) separada da **estimada** por duração (`aws/glue_collector.py`);
- CloudWatch CPU e Glue Observability (memória, disco, skew, executor gap);
- Spark event logs com limites explícitos, sem transformar evidência
  incompleta em zero sintético;
- crawlers, triggers, interactive sessions e DataBrew como tipos distintos;
- scanner estático de scripts dividido por evidência de runtime: com métrica
  correlata o padrão vira oportunidade bloqueada até benchmark A/B; sem ela vira
  **sinal** para a análise contextual, sem economia e fora do ranking;
- 9 modelos financeiros (`estimation/glue.py`) e custo por processo com rateio
  entre raízes compartilhadas;
- saúde da coleta por fonte, com impacto, próxima ação e categoria de erro
  estável, sem mensagens sensíveis;
- **custo real por usage type** (`aws/glue_cost.py`): `GetCostAndUsage` sobre a
  mesma janela do Athena, filtrado por `AWS Glue`, agrupado por `USAGE_TYPE`,
  classificado em buckets versionados (`etl_job`, `flex`, `crawler`,
  `interactive_session`, `databrew`, `catalog`, `data_quality`, `other`);
- **rateio por consumo medido** para jobs, crawlers, sessions e DataBrew, com
  `allocated_cost` e `cost_quality` por ativo;
- **gates de reconciliação**: inventário de jobs íntegro, `DPUSeconds` reportado
  acima da tolerância versionada, janela idêntica e razão consumo modelado ×
  cobrança dentro da banda 95%–105%;
- estimativas Glue ancoradas na cobrança quando existe alocação, com
  `baseline_quality` `allocated` / `allocated_partial` / `modeled`;
- `GLUE-UNATTRIBUTED-COST` nomeando os buckets sem rateio e os usage types não
  classificados, em vez do delta cego anterior.

### Pendente

- **custo real por job** via tags de alocação ativadas ou CUR, conforme a seção
  de granularidade acima;
- **Data Catalog e Data Quality** como categorias analisadas, não apenas
  reportadas como cobrança sem rateio;
- **jobs de streaming**: a DPU-hora é estimada por sobreposição de janela, sem
  regra de custo própria;
- **Ray / M-DPU**: tarifa e comportamento ainda não validados em conta real;
- **Price List API — conferência pendente em conta real.** As tarifas saíram do
  código para `knowledge/pricing/tables/sa-east-1.toml`, com procedência e a
  flag `verified`, hoje **falsa**: são os valores usados desde o MVP e ninguém
  os conferiu contra fonte citável. `julius pricing inspect` e
  `julius pricing refresh` regeram a tabela a partir da API, mas o mapeamento
  entre atributo de produto e tarifa (`knowledge/pricing/mapping.toml`) é um
  ponto de partida plausível, **não validado** — rodar `inspect` antes do
  primeiro `refresh` não é opcional.

## Redshift — escopo da coleta, e por quê

A coleta de Redshift fica no **plano de controle e no CloudWatch**:
`DescribeClusters`, `ListWorkgroups`, CPU, conexões e estado. Isso sustenta
regras de capacidade e ociosidade.

Não sustenta regra de query. Histórico de execução, skew de distribuição e
tabelas nunca lidas vivem em `SVV_*` e `STL_*`, alcançáveis só por conexão de
banco ou pela Redshift Data API — credencial de banco, permissão nova e um raio
de acesso diferente do resto da coleta, que hoje só fala com API de controle.

A decisão foi **manter o escopo limitado** até que ampliá-lo seja uma escolha
explícita. As consequências estão visíveis, não escondidas:

- `RedshiftCluster` não tem campo para o que não é medido. Um campo que sempre
  vale zero pareceria medido.
- `queries_in_window` é `None`, nunca `0`.
- As duas regras (`REDSHIFT-IDLE-CLUSTER`, `REDSHIFT-OVERSIZED`) nascem
  **bloqueadas**, com `saving_quality = unavailable`: aparecem no relatório,
  nomeiam a evidência que falta e não reservam economia.

Ampliar o escopo é adicionar um coletor que use a Data API e preencher os campos
correspondentes — as regras existentes passam a poder afirmar economia.

## Fontes de dados

Além das já listadas em [glue-analysis.md](glue-analysis.md):

- <https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html>
- <https://docs.aws.amazon.com/cur/latest/userguide/what-is-data-exports.html>
- <https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/custom-tags.html>
- <https://docs.aws.amazon.com/athena/latest/ug/reusing-query-results.html>
- <https://docs.aws.amazon.com/athena/latest/ug/workgroups-setting-control-limits-cloudwatch.html>
