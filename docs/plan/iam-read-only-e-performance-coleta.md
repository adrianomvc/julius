# Plano integrado — IAM read-only e performance da coleta

> **Data:** 2026-08-03
>
> **Estado:** em implantação. Fallback Athena por conta, cobertura de descoberta
> explícita e diagnóstico IAM estruturado do S3 já foram implementados
> localmente; aguardam revisão/publicação.
>
> **Escopo:** corrigir lacunas de cobertura causadas por IAM na conta Consumer e
> continuar a evolução de performance para contas com muitos serviços, buckets,
> objetos e processos.
>
> **Fora de escopo:** alterar IAM ou recursos AWS pelo Julius, habilitar logging,
> Storage Lens, Inventory ou qualquer outro serviço, enviar e-mail, executar
> comandos live sem aprovação humana e mudar valores do motor determinístico.

Este documento complementa
[`performance-coleta-contas-grandes.md`](performance-coleta-contas-grandes.md).
O bloqueio IAM vem antes do próximo benchmark: medir velocidade com fontes
incompletas produziria um baseline artificialmente rápido.

---

## 1. Resultado esperado

1. Toda permissão ausente aparece por operação, serviço, quantidade de recursos
   afetados e consequência analítica.
2. Uma operação negada nunca vira `False`, lista vazia ou custo zero.
3. Falhas opcionais não derrubam a conta inteira.
4. A coleta evita repetir trabalho inútil quando a mesma ação está globalmente
   bloqueada, sem esconder políticas diferentes por bucket.
5. A política proposta contém somente ações de leitura/listagem necessárias.
6. Após a cobertura ser recuperada, o batching CloudWatch e as demais ondas são
   comparados contra um baseline equivalente.

## 2. Bloqueios observados e nomes corretos

Os nomes abaixo separam método boto3, operação da API e ação IAM. Eles não são
intercambiáveis.

| Serviço | Método boto3 chamado pelo Julius | Ação IAM exigida | Escopo de recurso | Impacto da negação |
|---|---|---|---|---|
| S3 | `get_bucket_lifecycle_configuration` | `s3:GetLifecycleConfiguration` | `arn:aws:s3:::BUCKET` | lifecycle fica desconhecido; o Julius não pode afirmar que já existe transição/expiração |
| S3 | `get_bucket_logging` | `s3:GetBucketLogging` | `arn:aws:s3:::BUCKET` | destino de access logs fica desconhecido; evidência de leitura por objeto não pode ser localizada |
| S3 | `get_bucket_metadata_configuration` | `s3:GetBucketMetadataTableConfiguration` | `arn:aws:s3:::BUCKET` | não se sabe se há S3 Metadata; o nome IAM é diferente do método V2 |
| S3 | `list_bucket_analytics_configurations` | `s3:GetAnalyticsConfiguration` | `arn:aws:s3:::BUCKET` | Storage Class Analysis fica desconhecida |
| S3 | `list_bucket_intelligent_tiering_configurations` | `s3:GetIntelligentTieringConfiguration` | `arn:aws:s3:::BUCKET` | Intelligent-Tiering existente fica desconhecido |
| S3 Control | `list_storage_lens_configurations` | `s3:ListStorageLensConfigurations` | `*` | Storage Lens da conta fica desconhecido; a ação de listagem não oferece recurso específico na referência de autorização |
| Athena | `list_work_groups` | `athena:ListWorkGroups` | `*` | o Julius recua para `primary`; outros workgroups e históricos podem ficar fora da cobertura |

Referências oficiais:

- [Permissões exigidas pelas APIs S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html)
- [GetBucketMetadataConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketMetadataConfiguration.html)
- [GetBucketLifecycleConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketLifecycleConfiguration.html)
- [ListStorageLensConfigurations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_control_ListStorageLensConfigurations.html)
- [ListWorkGroups](https://docs.aws.amazon.com/athena/latest/APIReference/API_ListWorkGroups.html)
- [Ações e recursos do Athena](https://docs.aws.amazon.com/service-authorization/latest/reference/list_athena.html)

### 2.1 Correções de nomenclatura

Os nomes relatados devem ser normalizados antes de abrir o pedido IAM:

- `GetBucketListLifecycleConfigure` não é ação IAM; usar
  `s3:GetLifecycleConfiguration`.
- `GetBucketMetadataConfiguration` é a API V2, mas a ação IAM continua
  `s3:GetBucketMetadataTableConfiguration`.
- `GetAnalyticsConfiguration` e `GetIntelligentTieringConfiguration` autorizam
  as respectivas APIs `ListBucket...Configurations`.
- `ListWorkGroups`, no plural, corresponde a `athena:ListWorkGroups`.

## 3. Política mínima proposta para revisão humana

O Julius não aplica esta política. O bloco é insumo para o time responsável por
IAM revisar, restringir aos buckets Consumer e implantar pelo processo oficial.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "JuliusReadS3BucketConfiguration",
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketLogging",
        "s3:GetLifecycleConfiguration",
        "s3:GetAnalyticsConfiguration",
        "s3:GetIntelligentTieringConfiguration",
        "s3:GetBucketMetadataTableConfiguration"
      ],
      "Resource": [
        "arn:aws:s3:::BUCKET_CONSUMER_1",
        "arn:aws:s3:::BUCKET_CONSUMER_2"
      ]
    },
    {
      "Sid": "JuliusListAccountLevelS3Configuration",
      "Effect": "Allow",
      "Action": "s3:ListStorageLensConfigurations",
      "Resource": "*"
    },
    {
      "Sid": "JuliusListAthenaWorkGroups",
      "Effect": "Allow",
      "Action": "athena:ListWorkGroups",
      "Resource": "*"
    },
    {
      "Sid": "JuliusReadAthenaWorkGroups",
      "Effect": "Allow",
      "Action": "athena:GetWorkGroup",
      "Resource": "arn:aws:athena:sa-east-1:ACCOUNT_ID:workgroup/*"
    }
  ]
}
```

Regras para a revisão:

- substituir placeholders; nunca publicar a política com nomes genéricos;
- preferir a lista exata de buckets descoberta pelo escopo Consumer;
- manter ações `Put*`, `Create*`, `Update*`, `Delete*`, `Start*` e `Stop*` fora
  desta política;
- revisar SCP, permission boundary, session policy e bucket policy: um `Allow`
  na role não supera `Deny` explícito em outra camada;
- confirmar a região/home region usada pelo S3 Storage Lens;
- manter `Resource: "*"` somente nas ações de listagem que não suportam ARN de
  recurso específico;
- validar a política com IAM Access Analyzer no processo do time responsável.

## 4. Comportamento correto enquanto IAM não for liberado

### S3 Config

- cada campo negado permanece `None`, significando “não consultado”;
- ausência real (`NoSuchLifecycleConfiguration`, por exemplo) continua sendo
  resposta válida e pode virar lista vazia/`False`;
- a fonte fica `partial` ou `unavailable`, com `permission_denied`;
- recomendações dependentes de último acesso ou automação ficam bloqueadas ou
  pedem evidência;
- snapshot parcial nunca é salvo;
- durante a homologação IAM, executar sem `--snapshot-dir`, evitando que um
  snapshot válido de até 15 minutos esconda temporariamente uma mudança de
  permissão.

### Athena

- falha em `ListWorkGroups` pode usar `primary` apenas como fallback parcial;
- `primary` nunca representa “todos os workgroups” quando a listagem foi negada;
- cobertura, impacto e próxima ação devem aparecer na saúde da fonte;
- regras de custo, reutilização e output location não podem interpretar os
  workgroups ausentes como uso zero.

### 4.1 Workgroups conhecidos da conta Consumer

O escopo informado para esta conta contém três workgroups com papéis distintos:

| Workgroup | Papel operacional informado | Tratamento na coleta |
|---|---|---|
| `primary` | não utilizado | coletar para confirmar ausência de uso; nunca usá-lo sozinho como retrato da conta |
| `analytics-workgroup` | legado, com orientação para não usar, mas ainda ativo por alguns usuários | coletar integralmente; o uso residual é evidência relevante e não deve ser filtrado |
| `analytics-workgroup-v3` | padrão atual indicado ao time | coletar integralmente e comparar participação, configuração e atividade com o legado |

Consequências:

- o fallback atual apenas para `primary` é inadequado nesta conta;
- `analytics-workgroup` não deve ser excluído por ser legado: descobrir quem
  ainda o utiliza e qual consumo permanece é parte do diagnóstico;
- `analytics-workgroup-v3` não pode ser assumido como único workgroup ativo sem
  consultar a evidência;
- “preferido”, “legado” e “sem uso esperado” são metadados operacionais; uso,
  custo e configuração continuam vindo da coleta determinística.

Até `athena:ListWorkGroups` ser liberado, implementar uma configuração explícita
por conta com os três nomes. Essa configuração recupera os workgroups conhecidos,
mas a cobertura permanece `partial`: sem a listagem da conta, não há prova de que
um quarto workgroup não exista.

Configuração implementada no cadastro local (`schema_version: "1.3"`):

```json
{
  "schema_version": "1.3",
  "accounts": [{
    "name": "CONTA_CONSUMER",
    "expected_account_id": "ACCOUNT_ID",
    "sso_profile": "PERFIL_SSO",
    "enabled": true,
    "athena_workgroups": [
      {"name": "primary", "role": "unused_expected"},
      {"name": "analytics-workgroup", "role": "legacy"},
      {"name": "analytics-workgroup-v3", "role": "preferred"}
    ]
  }]
}
```

Também é possível sobrescrever o cadastro em uma execução:

```text
--athena-history-workgroups primary,analytics-workgroup,analytics-workgroup-v3
```

Cadastros `1.2` com apenas nomes continuam compatíveis e recebem o papel
`unclassified`. Os papéis são contexto operacional exibido no relatório; não
alteram uso, custo, confiança ou prioridade.

Quando `ListWorkGroups` funcionar, o Julius une descoberta e configuração:

- workgroup descoberto e configurado mantém seu papel operacional;
- workgroup descoberto e não configurado entra normalmente e é sinalizado para
  classificação;
- workgroup configurado e não retornado aparece como ausente/inacessível, não
  como zero;
- nenhum papel operacional altera sozinho custo, prioridade ou confiança.

## 5. Onda IAM-1 — diagnóstico estruturado

**Estado:** parcialmente implementada para `S3 Config`: ação IAM correta,
operação, serviço, contador de recursos e até três exemplos chegam ao dataset,
contexto da IA, relatório e Excel. A generalização para outras fontes permanece.

### Entrega

- substituir gaps textuais por registro estruturado contendo serviço, operação,
  ação IAM esperada, categoria, recursos afetados e até três exemplos;
- agregar cem buckets negados em uma ocorrência com contador, sem perder o
  alcance;
- separar `permission_denied`, `not_found`, `unsupported` e erro regional;
- incluir a ação IAM correta no `next_action`;
- expor o diagnóstico em JSON, relatório e Excel;
- registrar se o dado veio fresco ou de snapshot.

### Aceite

- nenhuma mensagem recomenda a ação IAM com nome incorreto;
- `AccessDenied` não vira configuração desligada;
- o relatório mostra quantos buckets foram afetados;
- conteúdo de mensagens AWS é sanitizado e não expõe credenciais/tokens;
- serial e paralelo produzem a mesma cobertura.

## 6. Onda IAM-2 — evitar repetição inútil de negações

**Estado:** a latência por bucket foi paralelizada com workers limitados,
resultados locais e agregação determinística; isso reduz tempo de parede sem
reduzir cobertura. O circuit breaker automático permanece pendente porque o
primeiro `AccessDenied` não prova negação global quando bucket policies, SCPs e
condições podem variar por recurso.

Não é seguro parar após o primeiro `AccessDenied`: bucket policies podem permitir
uma ação em um bucket e negar em outro. O controlador será conservador.

### Entrega

- classificar negação global somente quando a resposta provar que a identidade
  não possui a ação, sem inferir pelo primeiro bucket;
- para negação inequivocamente global, abrir o circuito daquela operação no
  restante do scan e marcar todos os recursos não tentados como indisponíveis;
- para negação por bucket, continuar os demais buckets sob limite de
  concorrência;
- Storage Lens continua uma chamada por conta, não por bucket;
- circuito isolado por operação: negar lifecycle não impede logging;
- novo scan volta a testar; estado de permissão não é cache permanente.

### Aceite

- política mista por bucket mantém os buckets permitidos;
- negação global em milhares de buckets não produz milhares de chamadas iguais;
- cobertura informa tentados, negados, pulados pelo circuito e bem-sucedidos;
- nenhuma fonte obrigatória é cancelada por uma permissão opcional.

## 7. Onda IAM-3 — Athena completo e fallback honesto

**Estado:** fallback por CLI/cadastro e união com descoberta implementados. A
descoberta negada permanece parcial e bloqueia reconciliação. Persistir papéis
operacionais e detalhar falha por workgroup permanecem.

### Entrega

- tornar falha de `ListWorkGroups` uma lacuna explícita em `AthenaCoverage`;
- substituir o fallback fixo `primary` pelos workgroups explícitos da conta;
- manter `primary` isolado apenas como compatibilidade quando não existir
  configuração explícita, sempre com cobertura parcial;
- unir configuração e descoberta quando `ListWorkGroups` estiver disponível;
- não calcular `workgroups_total=1` como total conhecido quando a listagem falha;
- testar paginação de até 50 workgroups por página;
- distinguir negação de `ListWorkGroups`, `GetWorkGroup`,
  `ListQueryExecutions` e `BatchGetQueryExecution`;
- garantir que um workgroup negado não zere os demais.
- preservar o papel `preferred`/`legacy` como contexto, sem transformá-lo em
  veredito determinístico de uso.

### Aceite

- múltiplos workgroups permitidos são todos coletados;
- listagem negada coleta os três workgroups conhecidos e produz cobertura
  parcial, mesmo que todos funcionem;
- falha em um `GetWorkGroup` não apaga execuções dos outros;
- regras que exigem cobertura total ficam bloqueadas quando apropriado.
- uso residual de `analytics-workgroup` permanece visível e atribuível;
- `analytics-workgroup-v3` aparece separado do legado em métricas e histórico.

## 8. Onda IAM-4 — homologação humana read-only

Somente após alteração IAM pelo time responsável e aprovação explícita na
máquina de trabalho:

1. validar identidade com STS;
2. executar uma chamada read-only de cada operação bloqueada;
3. executar `julius collect` sem snapshots;
4. confirmar desaparecimento apenas dos gaps realmente liberados;
5. comparar inventário/saúde antes e depois;
6. confirmar que nenhuma operação fora da allowlist foi chamada;
7. registrar duração e quantidade de chamadas negadas antes/depois.

Nenhuma etapa habilita recurso, altera bucket, muda workgroup ou envia e-mail.

## 9. Sequência integrada de performance

O trabalho de performance continua nesta ordem:

### P0 — cobertura IAM e baseline honesto

- Ondas IAM-1 a IAM-3;
- baseline serial e paralelo com a mesma cobertura;
- relatório de chamadas negadas e fontes parciais.

### P1 — batching global do CloudWatch

- planejador único de `GetMetricData` para Glue, SageMaker, Redshift e S3;
- deduplicação por namespace, métrica, dimensões, período e estatística;
- até o limite oficial por lote, com paginação por `NextToken`;
- IDs estáveis e mapeamento determinístico da resposta;
- resposta ausente/parcial nunca vira zero;
- limites e adaptação isolados para `cloudwatch`;
- telemetria de métricas planejadas, deduplicadas, chamadas e tempo economizado.

### P1 — S3 em contas grandes

- manter streaming e deduplicação de prefixos já implementados;
- adicionar adapter para S3 Inventory já existente;
- incluir futuramente `s3:GetInventoryConfiguration` somente quando o adapter
  estiver implementado e o time aprovar essa cobertura;
- nunca criar ou alterar Inventory;
- validar idade, manifesto, schema, bucket e prefixo antes de consumir;
- Inventory incompatível ou atrasado vira parcial e recua para o modo autorizado.

### P2 — incremental e retomada

- expandir snapshots apenas depois de separar configuração estável de métricas
  voláteis;
- checkpoints por Glue, Athena, S3, SageMaker, Redshift e orquestração;
- retomada de unidades válidas por conta/scan/hash;
- supersessão segura de jobs de IA de scans antigos.

### P2 — pipeline assíncrono da IA

- worker separado do pool boto3;
- fila limitada e persistente no `RunStore`;
- pacote imutável por checkpoint;
- validação de conta, scan, hash, prompt e artefatos;
- publicação determinística sem aguardar provider;
- merge enriquecido posterior sem alterar valores determinísticos.

### P3 — caminho crítico e backpressure

- prioridade para dependências que liberam mais fontes;
- limite de páginas em fila e memória;
- cancelamento de páginas opcionais antes do orçamento;
- degradação adaptativa por serviço;
- modo serial como rollback.

## 10. Estratégia de testes

### IAM/S3

- todas as seis ações permitidas;
- cada ação negada isoladamente;
- todas negadas globalmente;
- permissão mista entre buckets;
- `not_found` distinto de `permission_denied`;
- metadata V1/V2 usando a mesma ação IAM;
- Storage Lens negado sem contaminar configurações por bucket;
- snapshot válido, vencido e desabilitado durante homologação.

### IAM/Athena

- `ListWorkGroups` paginado;
- listagem negada com os três workgroups conhecidos configurados;
- descoberta de um quarto workgroup não configurado;
- workgroup configurado ausente ou sem permissão;
- `GetWorkGroup` negado em somente um workgroup;
- query history parcial por workgroup;
- cobertura não conhecida diferente de total igual a um.

### Performance

- equivalência serial/paralela;
- batching CloudWatch com resposta fora de ordem e `NextToken`;
- throttling isolado por serviço;
- milhares de buckets com negação global e mista;
- nenhuma explosão de memória em S3;
- IA lenta/indisponível sem aumentar duração da coleta.

Gates locais:

```text
pytest -q
ruff check .
mypy julius
git diff --check
```

Não haverá CI, conforme decisão do projeto.

## 11. Telemetria e critérios de sucesso

Por operação IAM:

- chamadas tentadas;
- sucessos, negações, `not_found` e erros;
- recursos afetados;
- chamadas evitadas por circuito comprovadamente global;
- ação IAM esperada;
- origem fresca/cache e idade.

Por scan:

- zero campo negado apresentado como desligado/zero;
- zero mistura entre contas;
- zero operação de mutação;
- mesma evidência entre serial e paralelo;
- menos chamadas após batching/circuit breaker;
- mesma cobertura antes e depois de otimização;
- relatório determinístico independente da IA.

Metas percentuais de duração e chamadas serão definidas somente após a
homologação na mesma conta, janela e cobertura.

## 12. Rollback

- omitir `--snapshot-dir` desliga snapshots;
- `--collection-execution serial` desliga o DAG concorrente;
- batching CloudWatch deve possuir caminho legado por fonte durante homologação;
- circuit breaker pode voltar a “tentar todos”, preservando cobertura;
- Inventory pode recuar para `bounded`/`full` autorizado;
- worker de IA pode ser desligado sem afetar coleta e motor determinístico;
- remover a política IAM proposta volta ao estado parcial anterior, sem alterar
  recursos AWS.

## 13. Backlog consolidado

| Prioridade | Item | Estado |
|---|---|---|
| P0 | Diagnóstico IAM estruturado e ação correta | Parcial: `S3 Config` pronto |
| P0 | Cobertura Athena honesta sem `ListWorkGroups` | Implementada localmente |
| P0 | Revisão/aplicação humana da política read-only | Externo; requer time IAM |
| P1 | Concorrência S3 Config sem contaminação | Implementada localmente |
| P1 | Evitar repetição de negação global comprovada | Planejado; exige prova segura |
| P1 | Batching global CloudWatch | Planejado após IAM-1/IAM-3 |
| P1 | S3 Inventory existente | Planejado |
| P2 | Expandir snapshots elegíveis | Parcial: `S3 Config` pronto |
| P2 | Checkpoints por domínio e retomada | Parcial: `RunStore` pronto |
| P2 | Worker assíncrono e merge da IA | Planejado |
| P3 | Prioridade por caminho crítico e memória | Planejado |
| P0 | Homologação read-only na conta | Bloqueada por acesso/aprovação |

## 14. Próxima execução de implementação

1. concluir a homologação local de concorrência e papéis Athena;
2. definir uma prova segura ou manifesto explícito para negação IAM global;
3. iniciar o planejador global CloudWatch;
4. homologar a policy read-only na conta somente com aprovação humana.
