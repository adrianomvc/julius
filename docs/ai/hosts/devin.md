# Procedimento operacional — DEVIN CLI

Este é o único arquivo específico de host. Ele descreve **como** executar o
Julius numa sessão Devin; **o que** analisar está na Skill canônica, que é a
mesma para qualquer host.

## Instalação

Confirme que o repositório é o Julius e instale com o instalador, não com um
`pip install` cru:

```bash
bash install/install.sh
```

Ele escolhe um Python 3.11+ (o `python3` do Ubuntu 22.04 é 3.10 e o projeto
precisa de `tomllib` e `StrEnum`), cria o virtualenv, instala o pacote e põe o
lançador `julius` no `PATH`.

**As duas únicas formas de invocar o Julius:**

```bash
julius <comando> [opções]              # depois de install/install.sh
python -m julius.cli <comando> [opções] # equivalente, sem depender do PATH
```

O arquivo `julius/cli.py` **não existe** — o CLI é o pacote `julius/cli/`, um
módulo por família de comandos. Se `julius` não for encontrado, ou a instalação
não rodou ou `~/.local/bin` não está no `PATH`: use `python -m julius.cli`, ou
rode o instalador de novo. Nunca invente uma terceira forma, e nunca chame um
arquivo de dentro de `julius/` diretamente.

## Identidade

Use apenas identidades AWS CLI SSO configuradas explicitamente para o Julius:

- a região do Julius é sempre `sa-east-1`;
- nunca use `--role-arn`;
- use `--sso-profile` só com perfil habilitado no registro local de contas;
- nunca leia, copie, imprima ou persista chaves, segredos, tokens de sessão ou
  arquivos do cache SSO.

Não liste perfis, não descubra contas via AWS Organizations e não amplie o escopo
implicitamente.

Exija `~/.julius-accounts.json` a partir de `.julius-accounts.example.json`. Ele
contém apenas o nome lógico da conta, o Account ID esperado, a referência
não-secreta ao perfil SSO e o `enabled` explícito. Verifique cada identidade
habilitada antes de coletar:

```bash
julius agent verify-accounts \
  --config ~/.julius-accounts.json \
  --output data/agent/verified-accounts.json
```

Antes de coletar uma conta configurada:

```bash
aws sts get-caller-identity --profile <sso-profile>
```

Registre conta e ARN, sem credenciais. Confirme que o Account ID bate com a
entrada habilitada e que o permission set SSO está documentado como read-only.
Pare em caso de divergência; não continue em silêncio sob outra identidade.

## Coleta

Use um dataset exportado quando houver. Para coleta ao vivo, escreva um dataset
por conta verificada:

```bash
julius collect --sso-profile <sso-profile> \
  --output data/collected/<account>.json
```

O comando sempre usa `sa-east-1` e a cadeia de credenciais ativa. Nunca reaproveite
o caminho de saída de uma conta para outra.

Colete os artefatos técnicos limitados com a mesma identidade verificada:

```bash
julius agent collect-artifacts \
  --input data/collected/<account>.json \
  --sso-profile <sso-profile> \
  --output data/artifacts/<account>
```

Este comando usa a mesma identidade SSO ativa e pode chamar apenas STS, S3
GetObject e operações de list/describe do Step Functions. Pare em divergência de
identidade.

## Pacote de análise

Gere um workspace por conta:

```bash
julius agent prepare \
  --input <dataset.json> \
  --output data/agent/<account> \
  --artifacts-manifest data/artifacts/<account>/manifest.json
```

Leia o `instructions.md`, o `context.json` e o `output-schema.json` daquela conta.
Analise só as oportunidades presentes naquele contexto e julgue só os sinais
presentes nele. Leia apenas os arquivos técnicos referenciados em
`technical_artifacts`. Confira `portfolio` para saber quanto da conta o pacote
cobre — é um recorte ranqueado, não o portfólio inteiro.

Escreva apenas o resultado estruturado em `result.json` e valide:

```bash
julius agent validate \
  --context data/agent/<account>/context.json \
  --result data/agent/<account>/result.json
```

Valide cada conta habilitada de forma independente antes de montar qualquer
resumo de portfólio. Nunca misture evidências ou `opportunity_id` entre contas.

## Artefatos finais

```bash
julius report \
  --input data/collected/<account>.json \
  --output data/reports/<account> \
  --artifacts-manifest data/artifacts/<account>/manifest.json \
  --agent-context data/agent/<account>/context.json \
  --agent-result data/agent/<account>/validated-result.json

julius notify \
  --mode dry-run \
  --input data/collected/<account>.json \
  --outbox data/outbox \
  --artifacts-manifest data/artifacts/<account>/manifest.json \
  --agent-context data/agent/<account>/context.json \
  --agent-result data/agent/<account>/validated-result.json
```

Envio ativo de e-mail é operação separada e aprovada por humano. Não use
`--mode active` como parte desta análise.

## Critérios de conclusão

O validador já cobre a maior parte deles, e o que ele cobre não se repete aqui.
Restam três que nenhum teste verifica e que dependem de julgamento:

- o diagnóstico explica **esta** ocorrência, e não o padrão em geral;
- a justificativa de cada veredito se sustenta sozinha para quem não leu o script;
- a ordem de implementação é coerente com as dependências declaradas.
