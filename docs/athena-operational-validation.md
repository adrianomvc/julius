# Homologação operacional do Athena

## Estado

A implementação do Athena está concluída e validada localmente. A homologação
abaixo depende da identidade AWS corporativa e é uma pendência operacional não
bloqueante para o desenvolvimento dos demais serviços.

O objetivo desta execução futura é validar a coleta e o custo em uma conta
real. Ela não autoriza alterações em workgroups, métricas, limites, consultas,
dados ou qualquer outro recurso AWS.

## Limites de segurança

- executar somente na região `sa-east-1`;
- usar apenas uma conta explicitamente habilitada em
  `~/.julius-accounts.json`;
- confirmar a identidade com STS antes da coleta;
- exigir que o permission set esteja documentado como read-only;
- nunca executar `EXPLAIN`, `EXPLAIN ANALYZE` ou uma query de teste;
- nunca habilitar métricas, result reuse ou controles do workgroup;
- nunca enviar e-mail ativo; usar somente `dry-run`;
- não transferir credenciais, cache SSO, SQL original, `QueryExecutionId`,
  eventos brutos do CloudTrail, IPs ou blocos completos de identidade.

## Opção A — homologação na máquina autorizada

### Pré-requisitos

- código correspondente ao `main` validado;
- Python suportado com `.[aws]` instalado;
- arquivo `~/.julius-accounts.json` baseado no exemplo do repositório;
- entrada da conta com `enabled: true`, Account ID esperado e `sso_profile`;
- sessão SSO válida e permission set read-only.

### Execução

Substitua `<conta>` pelo nome lógico cadastrado e `<perfil-sso>` pelo perfil
explicitamente autorizado.

```powershell
julius agent verify-accounts `
  --config ~/.julius-accounts.json `
  --output data/agent/verified-accounts.json

aws sts get-caller-identity --profile <perfil-sso>

julius collect --sso-profile <perfil-sso> `
  --output data/collected/<conta>.json

julius report `
  --input data/collected/<conta>.json `
  --output data/reports/<conta>

julius notify --mode dry-run `
  --input data/collected/<conta>.json `
  --outbox data/outbox/<conta>
```

Interrompa a execução se o Account ID retornado pelo STS não for exatamente o
esperado ou se não for possível confirmar que a identidade é read-only.

## Opção B — validação fora da máquina autorizada

Uma pessoa autorizada executa somente `verify-accounts` e `collect` na máquina
corporativa. Depois de revisar o arquivo normalizado, transfere apenas
`data/collected/<conta>.json` para o ambiente de validação.

No ambiente externo, executar somente:

```powershell
julius report `
  --input data/collected/<conta>.json `
  --output data/reports/<conta>

julius notify --mode dry-run `
  --input data/collected/<conta>.json `
  --outbox data/outbox/<conta>
```

Antes da transferência, confirmar que o dataset não contém SQL original,
`QueryExecutionId`, evento bruto do CloudTrail, IP, access key, token ou o
bloco completo `userIdentity`. Se a política corporativa não permitir a
transferência, toda a validação permanece na máquina autorizada.

Uma conta sandbox autorizada com atividade Athena preexistente também pode
validar a integração real das APIs. Ela não substitui a homologação da
cobertura, das permissões, das identidades e do custo da conta corporativa.

## Critérios de aprovação

### Identidade e cobertura

- STS corresponde à conta habilitada;
- período corresponde aos últimos 30 dias completos em UTC;
- todos os workgroups autorizados foram enumerados;
- paginação foi concluída por workgroup;
- cobertura real, data mais antiga, retenção, truncamentos e acessos negados
  aparecem no relatório;
- `SUCCEEDED`, `FAILED` e `CANCELLED` foram considerados.

### Custo e SAVE

- bytes faturáveis respeitam arredondamento por MB, mínimo de 10 MB, DDL,
  falhas, cancelamentos e result reuse;
- API do Athena e CloudWatch ficam entre 95% e 105%;
- custo usa `NetUnblendedCost`, ou registra explicitamente o fallback para
  `UnblendedCost`, na moeda retornada pela AWS;
- custo aparece como **custo líquido alocado**, sem sugerir que existe uma
  fatura por query;
- qualidade `reconciled` ocorre somente com cobertura integral, período
  idêntico e custo Athena on-demand isolado;
- fora desses gates, o relatório mostra custo parcial ou indisponível;
- SAVE de result reuse usa duplicatas exatas elegíveis;
- outras regras mostram faixa baixa, esperada e alta, identificada como
  estimativa modelada;
- oportunidades do mesmo padrão e orientações por pessoa não duplicam SAVE.

### Privacidade e artefatos

- nenhum SQL original ou histórico bruto foi persistido;
- nenhuma execução individual foi gravada no DuckDB;
- nenhuma informação proibida pelos limites de segurança aparece nos
  artefatos;
- existem somente `report.html`, `report.json`, `email.html` e `email.txt`;
- não existe `athena-report.*`;
- o e-mail foi apenas composto em `dry-run`.

## Registro do resultado

Preencher ao final sem incluir dados pessoais ou conteúdo de queries:

```text
Data UTC:
Conta lógica:
Commit Julius:
Identidade STS conferida: sim/não
Permission set read-only conferido: sim/não
Workgroups cobertos/total:
Janela UTC:
API/CloudWatch:
Fonte de custo:
Moeda:
Qualidade do custo:
Lacunas registradas:
Artefatos revisados:
Resultado: aprovado / aprovado com lacunas / reprovado
Responsável pela homologação:
```

## Referências oficiais

- [Histórico e paginação por workgroup](https://docs.aws.amazon.com/athena/latest/APIReference/API_ListQueryExecutions.html)
- [Registro de chamadas do Athena no CloudTrail](https://docs.aws.amazon.com/athena/latest/ug/monitor-with-cloudtrail.html)
- [Custos e uso no Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html)
