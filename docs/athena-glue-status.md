# Athena e Glue — estado, contrato de custo e pendências

Documento único de estado dos dois serviços que concentram a análise do Julius.
Complementa o [runbook de homologação Athena](athena-operational-validation.md)
e o [detalhamento técnico do Glue](glue-analysis.md).

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

## Athena

### Feito

- coleta mensal read-only por workgroup, com paginação completa e janela de 30
  dias UTC completos (`aws/athena_collector.py`);
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
  (`ATHENA_RECOVERY_RATES`), separando economia medida de estimativa modelada;
- integração completa em `report.html`, `report.json`, `email.html` e
  `email.txt`; cobertura em `tests/test_athena_monthly.py`.

### Pendente

- **homologação read-only em conta real** — depende de identidade corporativa;
  procedimento e critérios já descritos em
  [athena-operational-validation.md](athena-operational-validation.md);
- **capacidade provisionada**: `detectors/athena.py` ignora execuções com
  `modality != on_demand`; contas com capacity reservation não são analisadas;
- **controles de workgroup**: limite de dados por query, result reuse habilitado
  no próprio workgroup e migração para engine v3 ainda não viram regra;
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
- scanner estático determinístico de scripts, com achados bloqueados até
  benchmark A/B;
- 9 modelos financeiros (`estimation/glue.py`) e custo por processo com rateio
  entre raízes compartilhadas;
- saúde da coleta por fonte, com impacto, próxima ação e categoria de erro
  estável, sem mensagens sensíveis;
- **custo real por usage type** (`aws/glue_cost.py`): `GetCostAndUsage` diário
  do mês corrente filtrado por `AWS Glue`, agrupado por `USAGE_TYPE`,
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
- **Price List API**: as tarifas de `julius/config.py` são premissas
  versionadas; falta consulta a preço datado por região.

## Fontes de dados

Além das já listadas em [glue-analysis.md](glue-analysis.md):

- <https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html>
- <https://docs.aws.amazon.com/cur/latest/userguide/what-is-data-exports.html>
- <https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/custom-tags.html>
- <https://docs.aws.amazon.com/athena/latest/ug/reusing-query-results.html>
- <https://docs.aws.amazon.com/athena/latest/ug/workgroups-setting-control-limits-cloudwatch.html>
