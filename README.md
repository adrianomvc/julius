# Julius — MVP 4: IA no Devin

Portfólio contínuo de oportunidades de otimização de custo AWS para contas
Consumer (Data Mesh), usado como ferramenta especializada pelo agente Devin.
O **MVP 4** combina o motor determinístico com análise contextual por IA,
relatórios acionáveis e entrega segura por conta. O MVP 3 permanece como base
do ciclo fechado e os MVPs 1B e 2 como histórico, inventário e contexto.

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
- `report.html`, `report.json`, `email.html` e `email.txt`; composição de e-mail
  em `dry-run` por padrão.

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

## Julius como IA no Devin (MVP 4)

O usuário interage diretamente com o **Devin**, pelo CLI ou pela interface web.
O Devin encontra a Skill versionada em
`.agents/skills/julius-aws-analysis/SKILL.md` e usa o CLI Julius como sua
ferramenta especializada. O Julius não chama a API do Devin.

As responsabilidades são separadas:

- **Julius determinístico:** coleta, inventário, grafo, evidências, economia,
  dificuldade, confiança, prioridades, IDs e lifecycle;
- **Devin/IA:** leitura contextual de scripts, SQL e dependências, explicação da
  causa, sequência de implementação, conflitos, riscos e documentação oficial;
- **validador Julius:** impede que a saída da IA altere campos determinísticos,
  use IDs inexistentes ou referencie documentação fora de
  `docs.aws.amazon.com`.

Exemplo de interação no Devin:

```text
Use a Skill Julius AWS Analysis para analisar a conta Consumer.
Não altere nenhum recurso AWS. Entregue as recomendações priorizadas,
os passos de implementação e os links oficiais da AWS.
```

Dentro da sessão, o Devin executa:

```bash
julius agent collect-artifacts \
  --input data/collected/123456789012.json \
  --output data/artifacts/123456789012

julius agent prepare --input data/sample/consumer-avi.json --output data/agent
# Em conta real, adicionar:
# --artifacts-manifest data/artifacts/123456789012/manifest.json
# Devin lê context.json/instructions.md, analisa e grava result.json
julius agent validate \
  --context data/agent/context.json \
  --result data/agent/result.json

# O Devin gera os artefatos já enriquecidos pela análise validada
julius report \
  --input data/sample/consumer-avi.json \
  --agent-context data/agent/context.json \
  --agent-result data/agent/validated-result.json

# Prévia local; nunca envia durante a análise
julius notify --mode dry-run \
  --input data/sample/consumer-avi.json \
  --agent-context data/agent/context.json \
  --agent-result data/agent/validated-result.json
```

Esse mesmo fluxo funciona no Devin CLI e na web do Devin, porque a inteligência
e a conversa pertencem ao Devin; os comandos acima são ferramentas locais do
workspace. Os artefatos de `data/agent/` não são versionados.

Para preparar o workspace, use `scripts/bootstrap-devin.sh` no ambiente
Linux/Devin Cloud ou `scripts/bootstrap-devin.ps1` no PowerShell. Ambos criam a
`.venv`, instalam `.[aws,dev]`, executam os testes e fazem um smoke test com os
dados de exemplo; não acessam uma conta AWS durante o bootstrap.

### Uma ou várias contas AWS

Na máquina de trabalho, o Devin usa a cadeia de credenciais já configurada no
AWS CLI:

- sem perfil informado, analisa somente a identidade AWS ativa;
- com um perfil informado, analisa somente aquele perfil;
- quando o usuário pedir explicitamente **todas as contas**, lista os perfis do
  AWS CLI e processa cada um separadamente;
- assume-role só é usado quando o ARN for fornecido explicitamente.

Antes de coletar, o Devin executa `aws sts get-caller-identity` para cada
perfil/role e confere a conta. Cada conta recebe arquivos separados em
`data/collected/<conta>.json` e `data/agent/<conta>/`; evidências e IDs nunca
são misturados entre contas. A existência de vários perfis não autoriza
automaticamente analisar todos eles.

Para múltiplas contas, copie
[.julius-accounts.example.json](.julius-accounts.example.json) para
`~/.julius-accounts.json`, informe o ID esperado, perfil, região e role
opcional, e habilite somente as contas autorizadas. Antes da coleta:

```bash
julius agent verify-accounts \
  --config ~/.julius-accounts.json \
  --output data/agent/verified-accounts.json
```

O comando para na primeira divergência entre o perfil/role e o ID esperado.
Ele usa apenas `sts:GetCallerIdentity` e não descobre nem habilita contas
implicitamente.

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

## Notificações seguras (MVP 4)

O comando `julius notify` continua sempre em `dry-run` quando nenhum modo é
informado. Nesse modo ele apenas grava a mensagem, o relatório e um manifesto
idempotente em `data/outbox/`; SES e SMTP não são acessados.

O envio ativo existe para uso posterior na máquina de trabalho e exige, ao
mesmo tempo:

- `--mode active` e configuração local com `"mode": "active"`;
- remetente e domínios de destinatários autorizados;
- cadastro habilitado para a conta exata analisada;
- confirmação humana com `--confirm`, ou grupo previamente aprovado para uso
  não interativo;
- log persistente que impede o reenvio do mesmo scan para o mesmo grupo;
- scan sem erro crítico e relatório HTML gerado.

Use [.julius-email.example.json](.julius-email.example.json) para transporte e
allowlist, salvando a configuração local em `~/.julius-email.json`. Use
[.julius-recipients.example.json](.julius-recipients.example.json) para mapear
cada conta aos seus destinatários, salvando em
`~/.julius-recipients.json`. Esses arquivos não aceitam credenciais.

Cada cadastro de conta contém `to`, `cc`, `recipient_group` e `enabled`. O envio
ativo exige correspondência exata com o `account_id` do relatório; não existe
fallback para destinatários globais. Contas novas começam desabilitadas e
precisam ser habilitadas conscientemente.

SES usa a cadeia de credenciais AWS; SMTP lê somente
`JULIUS_SMTP_USERNAME` e `JULIUS_SMTP_PASSWORD` do ambiente.

```bash
# Apenas na máquina de trabalho, depois de revisar configuração e destinatários
julius notify --mode active --confirm
```

`--to` e `--recipient-group` são aceitos somente no `dry-run`. No envio ativo,
o Julius sempre usa o cadastro da conta para evitar encaminhamento manual ao
destinatário errado.

O transporte pode ser selecionado com `--transport ses` ou
`--transport smtp`. O envio real permanece parte das validações operacionais
adiadas; os testes locais usam clientes simulados.

Na coleta ao vivo, use `--cloudtrail` para atribuição de ator e
`--datawarm-job <identificador>` para reconhecer o publicador DataWarm.

## Validações adiadas para a máquina de trabalho

Estas etapas dependem de contexto corporativo e não bloqueiam a validação local:

- revisão humana real do Top 10;
- teste read-only em contas AWS reais;
- configuração da tabela de toques, do job DataWarm e do CloudTrail.
- validação de envio SES/SMTP com remetentes e destinatários corporativos.
- conexão do repositório ao Devin e descoberta da Skill Julius;
- execução da Skill com uma role AWS corporativa estritamente read-only.

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
