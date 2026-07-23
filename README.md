# Julius — MVP 3

Portfólio contínuo de oportunidades de otimização de custo AWS para contas
Consumer (Data Mesh). O **MVP 3** fecha o ciclo: detectar, decidir, implementar,
comparar execuções, validar o ganho realizado e calibrar estimativas futuras.
Os MVPs 1B e 2 permanecem como base histórica e de contexto do processo.

Ciclo do produto: **detectar → priorizar → recomendar → acompanhar → validar**.
Ver o plano completo (fases 1A→4) em `../.claude/plans/quero-criar-uma-ia-compiled-manatee.md`.

## O que o MVP 1B entrega

- 18 detectores para Glue, Interactive Sessions, Athena, dados, Step Functions
  e SageMaker.
- Portfólio multi-conta ordenado pela economia identificada.
- Agrupamento por ativo/causa raiz e fingerprint estável.
- Backlog operacional em JSON e snapshots analíticos em DuckDB/Parquet.
- Run manifest com versões, preços, fonte e configuração da execução.
- Revisão humana do Top 10, Precision@10 e taxa de falsos positivos.
- `report.html`, `report.json`, `email.html` e `email.txt`; envio permanece
  em dry-run até o MVP 4.

## O que o MVP 2 acrescenta

- Grafo tipado `schedule → Step Functions → Glue Job → tabela → Consumer/DataWarm`.
- Linhagem declarada e extração simples de tabelas em consultas Athena.
- Ownership por tag, cadastro corporativo, DataWarm, job escritor e comunidade.
- Pessoa/ator por tag Owner, CloudTrail `sourceIdentity` ou sessão SSO.
- Coletores de Step Functions, EventBridge, Glue Catalog e CloudTrail.
- Criticidade e alcance do processo anexados às oportunidades.
- Candidatura a Producer e prontidão de migração calculadas separadamente.

## O que o MVP 3 acrescenta

- Ciclo `detected → reviewed → accepted → planned → implemented → validated`.
- Rejeição (`dismissed`) sem repetição até surgir nova evidência.
- Diff entre execuções: novas, piora, nova evidência e desaparecimento.
- Validação prevista × realizada, com precisão e taxa de realização.
- Economia normalizada por volume para não confundir queda de demanda com ganho.
- Calibração por regra após pelo menos três benefícios validados.
- Eventos de lifecycle, diff e validações persistidos em DuckDB/Parquet.

## Como rodar

```bash
pip install -e .

# Ranking de uma conta
julius opportunities

# Gera os artefatos de uma conta
julius report

# Executa as três contas de exemplo e persiste DuckDB + Parquet
julius portfolio

# Lista o Top 10 pendente de revisão
julius review

# Exporta o grafo de processos da conta
julius graph

# Registra as etapas de uma oportunidade
julius lifecycle --opportunity-id <ID> --status accepted --actor <nome> --reason "<motivo>"
julius lifecycle --opportunity-id <ID> --status planned --actor <nome> --reason "<motivo>"
julius lifecycle --opportunity-id <ID> --status implemented --actor <nome> --reason "<motivo>"

# Compara a execução atual com o snapshot anterior
julius diff

# Valida benefício depois da implementação
julius validate --opportunity-id <ID> --baseline-cost 4500 --after-cost 3100 \
  --baseline-volume 10 --after-volume 5 --actor <nome>

# Registra a avaliação de uma recomendação
julius review --opportunity-id <ID> --verdict confirmed --reviewer <nome>
julius review --opportunity-id <ID> --verdict false-positive --reviewer <nome>

# Compõe o e-mail na outbox, sem envio real
julius notify --open-preview
```

Na coleta ao vivo, use `--cloudtrail` para atribuição de ator e
`--datawarm-job <identificador>` para reconhecer o publicador DataWarm.

## Validações adiadas para a máquina de trabalho

Estas etapas dependem de contexto corporativo e não bloqueiam a validação local:

- revisão humana real do Top 10;
- teste read-only em contas AWS reais;
- configuração da tabela de toques, do job DataWarm e do CloudTrail.

Até lá, testes e demonstrações usam somente os datasets de `data/sample/` e
históricos temporários, sem acesso à AWS e sem avaliações humanas fictícias.

O histórico padrão fica em `data/state/julius.duckdb`; os Parquets ficam em
`data/state/parquet/`. O backlog operacional permanece em
`data/state/backlog.json`.

## Princípios
- **Determinístico**: ganho, dificuldade, confiança, prioridade, IDs e buckets são
  calculados em código (não por IA). Preços/limiares versionados em `julius/config.py`.
- **80/20 em dois cortes**: financeiro (~80% da economia) e executável (o que dá
  para fazer já).
- **Linguagem de incerteza**: "estimamos ~R$ X/mês assumindo mesmo volume; será
  validado após a mudança".
- **Gate de acionabilidade**: sem ativo/evidência/ação/validação/responsável a
  oportunidade vai para *Investigações necessárias* (bucket `investigar_primeiro`).
- **E-mail = plano de ação; `report.html` = evidência.** Design em `design/`.

## Dados de entrada

`data/sample/` contém três contas Consumer para a prova multi-conta. O comando
`julius collect` também coleta dados ao vivo com boto3 e grava o mesmo schema
normalizado usado pelos datasets exportados.

## Testes
```bash
pip install pytest
PYTHONPATH=. python -m pytest -q tests/
```
