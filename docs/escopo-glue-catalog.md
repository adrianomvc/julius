# Escopo do Glue Catalog numa conta Consumer

> Qual recorte do catálogo pertence à conta analisada, e por que percorrer o resto custa chamada e devolve tabela sobre a qual ninguém pode agir. Extraído do README para manter lá o que todo leitor precisa.

Numa conta Consumer do Data Mesh o Glue Catalog enxerga bancos compartilhados
por **outras** contas. Percorrê-los custa uma chamada por banco e devolve
tabelas sobre as quais esta conta não pode agir: não gera, não desliga, não
redimensiona.

São **três** os bancos da conta, e eles não têm a mesma forma:

| Banco | Como é identificado |
| --- | --- |
| `database_db_compartilhado_consumer_<conta>` | carrega o nome da conta |
| `workspace_db` | nome fixo, igual em toda conta |
| `sagemaker_featurestore` | nome fixo, igual em toda conta |

O nome lógico da conta resolve o primeiro; os outros dois entram por nome fixo.
Uma regra de sufixo pegaria só o compartilhado e deixaria os outros dois de fora.

A comparação ignora maiúsculas e trata `-` e `_` como o mesmo separador, mas
**não** o descarta: sem ele, `..._consumer_navi` passaria pela conta `avi`. O
`collect` resolve o nome pelo `sso_profile` em `~/.julius-accounts.json`; se o
cadastro não existir, usa o próprio perfil como fallback. `--account-name`
continua disponível para sobrescrever a resolução. Os sufixos finais `-pro` e
`-prod` do nome cadastrado não fazem parte do nome do banco compartilhado:
`consumeratendimentodataservice-pro` seleciona
`database_db_compartilhado_consumer_atendimentodataservice`. Para um ambiente
sem essa convenção, `--glue-databases banco1,banco2` substitui a regra inteira.

Sem nenhum dos dois o comportamento é o antigo — todos os bancos — e a saúde da
coleta registra isso na fonte **Glue Catalog Scope**, que mostra quantos bancos
entraram de quantos vistos e por qual regra. Menos tabelas por escopo e menos
tabelas por permissão faltando se parecem no relatório; essa linha é o que
separa as duas.

Access key, secret, token e cache SSO nunca devem ser copiados para o
repositório nem para os arquivos Julius. O boto3/AWS CLI lê essas credenciais
do armazenamento local gerenciado pelo AWS CLI.

Copie [.julius-accounts.example.json](.julius-accounts.example.json) para
`~/.julius-accounts.json`, informe somente o nome lógico e o Account ID
esperado, associe o `sso_profile` e habilite somente as contas autorizadas.
O schema 1.1 aceita `scope_profile`. Contas cadastradas sem o campo usam
`consumer_datamesh`; datasets antigos sem metadado de escopo preservam
`full_analysis`. A linha de comando pode sobrescrever com
`--scope-profile consumer_datamesh|full_analysis`.

No perfil Consumer, Crawlers e DataBrew ficam `not_applicable` antes da criação
de qualquer cliente AWS. Redshift usa somente plano de controle, CloudWatch,
Cost Explorer, Advisor e guardrails Serverless; não acessa banco nem system
views. S3 opera em `storage_class_only`: pode recomendar ao owner uma mudança de
Storage Class por `CopyObject`, mas não Lifecycle, exclusão ou aborto de
multipart uploads. Small files só vira oportunidade quando existe processo
produtor ou consumidor identificado.
O orçamento opcional `--max-scan-cost <USD>` é um limite estimado e interrompe
novas fontes opcionais quando o custo acumulado o alcança; uma fonte já iniciada
pode ultrapassá-lo. Chamadas, páginas, retries, throttles, cache hits,
duração e operações ainda sem tarifa ficam no dataset e no run manifest.
