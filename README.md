# Julius — MVP 4: IA no Devin

Portfólio contínuo de oportunidades de otimização de custo AWS para contas
Consumer (Data Mesh), usado como ferramenta especializada pelo agente Devin.
O **MVP 4** combina o motor determinístico com análise contextual por IA,
relatórios acionáveis e entrega segura por conta. O MVP 3 permanece como base
do ciclo fechado e os MVPs 1B e 2 como histórico, inventário e contexto.

Ciclo do produto: **detectar → priorizar → recomendar → acompanhar → validar**.
Ver o plano completo (fases 1A→4) em `../.claude/plans/quero-criar-uma-ia-compiled-manatee.md`.

## O que o MVP 1B entrega

- Detectores versionados para Glue, código Glue, Interactive Sessions, Athena,
  dados, Step Functions e SageMaker.
- Portfólio multi-conta ordenado pela economia identificada.
- Agrupamento por ativo/causa raiz e fingerprint estável.
- Backlog operacional em JSON e snapshots analíticos em DuckDB/Parquet.
- Run manifest com versões, preços, fonte e configuração da execução.
- Saúde da coleta por fonte, com cobertura, atualização, impacto e erros
  categorizados sem mensagens sensíveis.
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

## O Julius analisa e recomenda — ele não altera nada

Toda recomendação é instrução para o time dono. O Julius nunca apaga objeto,
pausa cluster, reduz worker ou muda configuração de nenhum recurso, e isso não é
uma promessa no texto: é o que `tests/test_read_only.py` cobra.

A garantia é por **allowlist**: existe uma lista explícita de operações AWS que
o Julius tem permissão de chamar, cada uma com o motivo escrito ao lado. Qualquer
chamada nova falha no teste até alguém justificá-la ali. Proibir `delete_object`
deixaria `delete_objects` passar; permitir só o que está escrito não deixa.

**Uma única operação age, e está declarada:** `start_query_execution` roda um
SELECT no workgroup do Julius para ler a tabela de toques. Não altera dado, mas
custa bytes varridos e grava resultado em S3 — por isso a fonte é opcional
(`--touches-table`), a consulta é validada contra qualquer palavra-chave de
escrita, e o nome da tabela é verificado antes de entrar no SQL.

O envio de e-mail é a única ação para fora, e já tinha porteiro próprio: modo
explícito, configuração local, cadastro da conta, confirmação humana e log que
impede reenvio. O teste garante que o transporte não é alcançável sem passar
pela política.

O Julius também não remove nenhum arquivo local. Ele escreve relatório, backlog
e histórico; não apaga nada de ninguém.

O critério da divisão é o grau de certeza, não o serviço. **O determinístico é
para o que fecha**: config declarada mais métrica medida levam a uma ação única,
e a economia sai do próprio fato. **A IA é para o que tem N variáveis**: um
`collect()` sobre cem linhas é correto e sobre cem milhões é desperdício, e nem
o AST nem um limiar distinguem os dois.

- **Julius determinístico:** coleta, inventário, grafo, evidências, economia,
  dificuldade, confiança, prioridades, IDs e lifecycle. O scanner estático de
  scripts Glue produz achado quando existe métrica de runtime que o corrobore;
  sem ela, produz **sinal**;
- **sinais:** hipóteses rastreáveis (hash do artefato, linhas, evidência que
  falta) que não entram no backlog, não recebem economia e não disputam posição
  no ranking;
- **Devin/IA:** julga cada sinal contra o artefato completo, enriquece as
  oportunidades determinísticas com causa, sequência, conflitos e documentação
  oficial, decide o lado do trade-off quando a recomendação admite dois
  caminhos, e registra em `uncovered_findings` o desperdício que nenhuma regra
  do catálogo cobre;
- **validador Julius:** impede que a saída da IA altere campos determinísticos,
  use IDs inexistentes ou referencie documentação fora de
  `docs.aws.amazon.com`; exige veredito para **todo** sinal enviado, conteúdo
  não-vazio em cada recomendação, e `sha256` do artefato em qualquer conclusão
  sobre código.

Achado que mede a qualidade da coleta ou do processo — cobrança não atribuída,
divergência entre cron e execuções — recebe `category="inventory_integrity"` e
aparece em seção própria do relatório, fora do portfólio e do ranking.

Um padrão fora do catálogo que reaparece entre scans e contas é acumulado em
`data/state/rule-candidates.json` com `occurrences`. Uma vez só é anedota; o
mesmo padrão em várias contas é regra determinística esperando ser escrita.

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
  --artifacts-manifest data/artifacts/123456789012/manifest.json \
  --agent-context data/agent/context.json \
  --agent-result data/agent/validated-result.json

# Prévia local; nunca envia durante a análise
julius notify --mode dry-run \
  --input data/sample/consumer-avi.json \
  --artifacts-manifest data/artifacts/123456789012/manifest.json \
  --agent-context data/agent/context.json \
  --agent-result data/agent/validated-result.json
```

Esse mesmo fluxo funciona no Devin CLI e na web do Devin, porque a inteligência
e a conversa pertencem ao Devin; os comandos acima são ferramentas locais do
workspace. Os artefatos de `data/agent/` não são versionados.

O contexto está em `schema_version` **1.1**, que acrescentou `signals`,
`portfolio` e `constraints.rule_families_without_evidence`. Um `context.json`
gravado na versão 1.0 é recusado por `agent validate`: rode `agent prepare` de
novo em vez de reaproveitar o pacote antigo. `agent validate` também grava a
fila de candidatos a regra em `data/state/rule-candidates.json`; use
`--rule-candidates` para mudar o destino.

Para preparar o workspace, rode `bash install/install.sh`. Ele escolhe um Python
3.11+, cria a `.venv`, instala `.[aws,dev]`, publica o lançador `julius` e a
skill `julius-aws-analysis` no DEVIN CLI, cria os arquivos `~/.julius-*.json`
desabilitados e valida tudo com a suíte de testes e um smoke que gera o
`report.html`. Não acessa conta AWS nenhuma durante a instalação. Os detalhes,
inclusive como preencher o assistente de setup do Devin, estão em
[install/README.md](install/README.md).

### Conta AWS via SSO

Na máquina de trabalho, o Devin usa somente a identidade SSO já ativa na cadeia
de credenciais do AWS CLI. A região do Julius é fixa em **São Paulo
(`sa-east-1`)** e não há `role_arn`.

Um *profile name* é apenas o apelido de uma configuração no arquivo
`~/.aws/config`; ele não é uma credencial. No cadastro Julius, esse campo se
chama `sso_profile` e apenas referencia a configuração criada por
`aws configure sso`. Para uma única conta configurada como `default`, ele pode
ficar vazio. Para várias contas, cada entrada usa o seu perfil SSO.

```bash
aws sso login --profile <perfil-sso>
julius collect --sso-profile <perfil-sso> \
  --output data/collected/<conta>.json
```

Access key, secret, token e cache SSO nunca devem ser copiados para o
repositório nem para os arquivos Julius. O boto3/AWS CLI lê essas credenciais
do armazenamento local gerenciado pelo AWS CLI.

Copie [.julius-accounts.example.json](.julius-accounts.example.json) para
`~/.julius-accounts.json`, informe somente o nome lógico e o Account ID
esperado, associe o `sso_profile` e habilite somente as contas autorizadas.
Antes da coleta:

```bash
julius agent verify-accounts \
  --config ~/.julius-accounts.json \
  --output data/agent/verified-accounts.json
```

O comando abre cada perfil SSO habilitado, compara a identidade com o Account
ID esperado e para em caso de divergência. Ele usa apenas
`sts:GetCallerIdentity`, não descobre contas e não armazena credenciais.

## Como rodar

```bash
bash install/install.sh   # uma vez por máquina; ver install/README.md

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
idempotente em `data/outbox/`; o SMTP não é acessado.

O envio ativo existe para uso posterior na máquina de trabalho e exige, ao
mesmo tempo:

- `--mode active` e configuração local com `"mode": "active"`;
- remetente e domínios de destinatários autorizados;
- cadastro habilitado para a conta exata analisada;
- confirmação humana com `--confirm`, ou grupo previamente aprovado para uso
  não interativo;
- log persistente que impede o reenvio do mesmo scan para o mesmo grupo;
- scan sem erro crítico e relatório HTML gerado.

Use [.julius-email.example.json](.julius-email.example.json) para o relay SMTP
local e a allowlist, salvando a configuração em `~/.julius-email.json`. Use
[.julius-recipients.example.json](.julius-recipients.example.json) para mapear
cada conta aos seus destinatários, salvando em
`~/.julius-recipients.json`. Esses arquivos não aceitam credenciais.

Cada cadastro de conta contém `to`, `cc`, `recipient_group` e `enabled`. O envio
ativo exige correspondência exata com o `account_id` do relatório; não existe
fallback para destinatários globais. Contas novas começam desabilitadas e
precisam ser habilitadas conscientemente.

O envio não usa a conta AWS. A própria máquina abre a conexão SMTP pela
biblioteca Python `smtplib`. Quando o relay exigir autenticação, usuário e senha
são lidos somente de `JULIUS_SMTP_USERNAME` e `JULIUS_SMTP_PASSWORD`.

```bash
# Apenas na máquina de trabalho, depois de revisar configuração e destinatários
julius notify --mode active --confirm
```

`--to` e `--recipient-group` são aceitos somente no `dry-run`. No envio ativo,
o Julius sempre usa o cadastro da conta para evitar encaminhamento manual ao
destinatário errado.

O envio real permanece parte das validações operacionais adiadas; os testes
locais usam um cliente SMTP simulado.

Na coleta ao vivo, use `--cloudtrail` para atribuição de ator e
`--datawarm-job <identificador>` para reconhecer o publicador DataWarm.

## Validações adiadas para a máquina de trabalho

Estas etapas dependem de contexto corporativo e não bloqueiam a validação local:

- revisão humana real do Top 10;
- homologação read-only do Athena em conta real, conforme o
  [runbook operacional](docs/athena-operational-validation.md);
- configuração da tabela de toques, do job DataWarm e do CloudTrail.
- validação de envio SMTP local com remetentes e destinatários corporativos.
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
- **Linguagem de incerteza**: "estimamos ~US$ X/mês assumindo mesmo volume; será
  validado após a mudança".
- **Gate de acionabilidade**: sem ativo/evidência/ação/validação/responsável a
  oportunidade vai para *Investigações necessárias* (bucket `investigar_primeiro`).
- **E-mail = plano de ação; `report.html` = evidência.** Design em `design/`.

## Dados de entrada

`data/sample/` contém três contas Consumer para a prova multi-conta. O comando
`julius collect` também coleta dados ao vivo com boto3 e grava o mesmo schema
normalizado usado pelos datasets exportados.

## Testes

O `install/install.sh` já roda a suíte no fim da instalação. Para rodar de novo,
com a `.venv` que ele criou:

```bash
.venv/bin/python -m pytest -q          # .venv/Scripts/python.exe no Windows
.venv/bin/ruff check . && .venv/bin/mypy julius
```
