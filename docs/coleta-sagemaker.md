# Coleta do SageMaker, e as evidências adicionais de Glue, Athena e S3

> O que a coleta read-only inventaria, e o que cada evidência adicional permite concluir. Extraído do README para manter lá o que todo leitor precisa.

A coleta read-only inventaria Domains, Spaces e storage EBS do Studio, Apps,
endpoints e todas as variantes, inference components, Notebook Instances,
Training/Processing/Transform Jobs, Feature Store, Pipelines, schedules já
existentes de Model Monitor e resultados do Inference Recommender. Métricas EFS
do Domain permitem sinalizar storage sem I/O. Cost Explorer ancora os custos por
componente; storage do Space só recebe rateio quando o `UsageType` identifica
explicitamente volume do Studio. Space e EFS ociosos só exibem potencial quando
há custo rateado, histórico maduro e ausência de atividade; continuam bloqueados
até validar owner, retenção, backup e consumidores externos. A janela financeira
do Cost Explorer é mantida separada dos 90 dias de telemetria.

Por padrão, todos os jobs entram no inventário e os 100 de maior custo potencial
recebem métricas detalhadas (com mínimo por tipo). Para detalhar todos:

```bash
julius collect --sso-profile <perfil-sso> \
  --sagemaker-full-metrics \
  --output data/collected/<conta>.json
```

O mesmo arquivo de `--output` funciona como checkpoint: uma condição financeira
vira recomendação determinística após 90 dias de cobertura ou três coletas
consistentes. Falha isolada de job mostra o custo ocorrido, mas não o anualiza.
G3, Spot, escolha de modalidade, rightsizing, TTL e Savings Plans permanecem
sinais para análise contextual. Falta de permissão em uma dessas fontes deixa a
coleta parcial e não interrompe as demais.

## Evidências adicionais de Glue, Athena e S3

Para Glue Jobs, o inventário mantém a última execução mesmo quando ela está fora
da janela financeira. Isso permite separar job inativo há 90 dias, abandonado
há um ano e histórico sem permissão. A análise também expõe observability,
continuous logging, bookmarks, custo de falhas, saída medida em small files e
um experimento de rightsizing com candidatos entre 2 e 10 workers. Nenhuma
configuração é alterada pelo Julius. Bookmarks só recebem valor financeiro
quando a releitura redundante foi medida. Rightsizing permanece potencial até
três execuções controladas com a mesma entrada e saída validada.

No Athena, `ResultReuseConfiguration` solicitado pelo cliente é registrado
separadamente do resultado que foi efetivamente reutilizado. O reuse é sugerido
no cliente que submete a consulta, apenas para repetições exatas elegíveis; não
é tratado como chave global do workgroup. Managed Query Results, múltiplos
catálogos, Lake Formation e funções não determinísticas bloqueiam a sugestão.
Recomendações de tabela não particionada incluem uma proposta CTAS com formato
colunar, compressão e `partitioned_by`; o Julius não executa a SQL nem atribui
economia antes de medir um piloto comparável.

No S3, a coleta de Cost Explorer preserva, por `UsageType`, o
`UsageQuantity` e sua unidade ao lado do custo. Assim, requests como GET podem
ser mostrados como contagem e custo sem tratar unidades incompatíveis como se
fossem somáveis. A oportunidade de small files usa GETs agregados dos Server
Access Logs e custo unitário de `Requests-Tier2`; como os logs são best-effort,
o valor fica como potencial bloqueado, não entra na economia financeira.
