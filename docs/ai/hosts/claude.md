# Procedimento operacional — Claude Code

Este arquivo é específico de host. **O que** analisar está na Skill canônica, que
é a mesma para qualquer host; aqui está só **como** executar.

Se você está comparando este arquivo com `devin.md`, a diferença é curta de
propósito: caminho de instalação, forma de invocar e o que o host expõe. Toda
regra, todo contrato e toda pergunta são idênticos, porque vêm do mesmo corpo
canônico.

## Instalação

A Skill é instalada em `~/.claude/skills/julius-aws-analysis/SKILL.md` (usuário) ou
em `.claude/skills/julius-aws-analysis/SKILL.md` (projeto). O arquivo do
repositório é artefato gerado — para mudar o que ele diz, edite `docs/ai/` e rode
`python scripts/generate_skill_registry.py`.

O CLI vem do mesmo instalador:

```bash
bash install/install.sh
```

**As duas únicas formas de invocar o Julius:**

```bash
julius <comando> [opções]              # depois de install/install.sh
python -m julius.cli <comando> [opções] # equivalente, sem depender do PATH
```

O arquivo `julius/cli.py` **não existe** — o CLI é o pacote `julius/cli/`. Se
`julius` não for encontrado, use `python -m julius.cli`. Nunca invente uma
terceira forma, e nunca chame um arquivo de dentro de `julius/` diretamente.

## Identidade

Idêntica ao outro host, e não negociável:

- a região do Julius é sempre `sa-east-1`;
- nunca use `--role-arn`;
- use `--sso-profile` só com perfil habilitado no registro local de contas;
- nunca leia, copie, imprima ou persista chaves, segredos, tokens de sessão ou
  arquivos do cache SSO.

```bash
julius agent verify-accounts \
  --config ~/.julius-accounts.json \
  --output data/agent/verified-accounts.json

aws sts get-caller-identity --profile <sso-profile>
```

Confirme que o Account ID bate com a entrada habilitada e que o permission set SSO
está documentado como read-only. Pare em divergência; não continue em silêncio sob
outra identidade.

## Coleta e análise

```bash
julius collect --sso-profile <sso-profile> \
  --output data/collected/<account>.json

julius agent collect-artifacts \
  --input data/collected/<account>.json \
  --sso-profile <sso-profile> \
  --output data/artifacts/<account>

julius agent prepare \
  --input data/collected/<account>.json \
  --output data/agent/<account> \
  --artifacts-manifest data/artifacts/<account>/manifest.json
```

Leia o `instructions.md`, o `context.json` e o `output-schema.json` daquela conta.
Analise só as oportunidades presentes naquele contexto e julgue só os sinais
presentes nele. Leia apenas os arquivos referenciados em `technical_artifacts`.

Escreva o resultado estruturado em `result.json` e valide:

```bash
julius agent validate \
  --context data/agent/<account>/context.json \
  --result data/agent/<account>/result.json
```

## Artefatos finais

```bash
julius report \
  --input data/collected/<account>.json \
  --output data/reports/<account> \
  --artifacts-manifest data/artifacts/<account>/manifest.json \
  --agent-context data/agent/<account>/context.json \
  --agent-result data/agent/<account>/validated-result.json
```

Envio ativo de e-mail é operação separada e aprovada por humano. Não use
`--mode active` como parte desta análise.

## Uma diferença que importa

O Claude Code roda com ferramentas de leitura e escrita no repositório local. Isso
**não** amplia o que o Julius pode fazer na conta AWS: a allowlist de operações
está no pacote Python e vale igual, venha o comando de onde vier. Escrever no
repositório é escrever relatório e resultado; não é tocar na conta.
