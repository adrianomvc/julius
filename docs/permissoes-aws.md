# Permissões AWS que o Julius precisa

A política completa está em
[`install/julius-readonly-policy.json`](../install/julius-readonly-policy.json):
**52 ações, todas de leitura**. Ela é derivada da allowlist de
`tests/test_read_only.py`, que é a lista autoritativa do que o Julius tem
permissão de chamar — e um teste cobra que as duas continuem batendo.

```bash
aws iam create-policy \
  --policy-name JuliusReadOnly \
  --policy-document file://install/julius-readonly-policy.json
```

Anexe ao permission set do SSO que o Julius usa.

## A única fonte que derruba o scan inteiro

`Glue Jobs` é a **única** fonte marcada como obrigatória. Sem ela o Julius
levanta `RequiredCollectionError` e aborta, em vez de produzir um relatório
incompleto que parece completo: o rateio da fatura Glue, o grafo de processos e
quase todas as regras dependem do inventário de jobs.

| Permissão | Sem ela |
| --- | --- |
| `glue:GetJobs` | **a coleta aborta** |
| `glue:GetJobRuns` | **a coleta aborta** |

Todas as outras fontes degradam: a entrada some do portfólio, aparece na saúde
da coleta com categoria `permission_denied` e o relatório diz o que ficou sem
avaliar. É a diferença entre "não achamos desperdício aqui" e "não olhamos
aqui", e o produto nunca troca a segunda pela primeira.

Para diagnosticar um `permission_denied`, o comando é direto:

```bash
aws glue get-jobs --max-results 1 --profile <perfil> --region sa-east-1
```

## O que cada permissão destrava

| Serviço | Ações | O que deixa de existir sem elas |
| --- | --- | --- |
| `sts` | `GetCallerIdentity` | verificação de identidade — a coleta nem começa |
| `glue` | 13 ações | inventário de jobs, crawlers, triggers, sessões e catálogo |
| `athena` | 7 ações | padrões de query, particionamento, custo por padrão |
| `ce` | 2 ações | cobrança real; sem ela toda economia fica modelada por tarifa |
| `cloudwatch` | 2 ações | CPU, memória, disco, skew — **as regras de capacidade** |
| `s3` | 5 ações | multipart abandonado, resultado acumulado, arquivos pequenos |
| `states` | 4 ações | transições contadas, Standard→Express, loop de polling |
| `sagemaker` | 6 ações | app ocioso, endpoint sem invocação |
| `redshift` | 2 ações | cluster ocioso e dimensionamento |
| `databrew` | 3 ações | custo e falhas do DataBrew |
| `events` | 2 ações | frequência esperada dos processos |
| `cloudtrail` | `LookupEvents` | atribuição de autoria (opcional, `--cloudtrail`) |
| `identitystore` | 2 ações | ator vira pessoa em vez de identificador técnico |
| `pricing` | `GetProducts` | regeneração da tabela de preços |
| `application-autoscaling` | `DescribeScalableTargets` | autoscaling declarado dos endpoints |

## A única ação que age

`athena:StartQueryExecution` é a exceção declarada da allowlist. Ela roda um
**SELECT** no workgroup do Julius para ler a tabela de toques. Não altera dado,
mas custa bytes varridos e grava resultado em S3 — por isso a fonte é opcional
(`--touches-table`), o SQL é validado contra qualquer palavra-chave de escrita, e
o nome da tabela é verificado antes de entrar na consulta.

Se a tabela de toques não for usada, essa ação pode sair da política sem afetar
mais nada.

## `glue:GetPartitions` e por que ela está aqui

Ela é chamada por paginador (`get_paginator("get_partitions")`), e o teste de
allowlist só enxerga chamada direta de atributo no cliente — então essa operação
roda sem estar na lista que o `tests/test_read_only.py` cobra. Não é buraco de
segurança: paginador aqui só embrulha leitura. Mas é uma lacuna na garantia que
aquele arquivo afirma, e a política precisa dela de qualquer forma, senão a
contagem de partições falha em silêncio.
