---
name: julius-signal-economic-analysis
description: Produz uma faixa de ordem de grandeza para sinal confirmado cujo desperdício é real e para o qual nenhuma fórmula do motor fecha, sempre separada da economia oficial.
metadata:
  # Gerado a partir de docs/ai/ e do motor — não edite.
  trigger: Ativar quando um sinal confirmado tiver rule_id na lista de elegíveis a faixa contextual e o motor não tiver método de estimativa para ele.
  sections_to_load:
    - does
    - does not
    - rules
    - evidence requirements
    - output contract
  prompt_version: 2.2.0
  allowed_estimation_methods:
    - glue_interactive_capacity_reduction_v1
    - glue_shuffle_reduction_v1
    - sagemaker_gpu_to_cpu_instance_v1
    - sagemaker_managed_spot_training_v1
    - sfn_standard_to_express_v1
  estimation_methods_by_rule:
    GLUE-CODE-SHUFFLE: glue_shuffle_reduction_v1
    GLUE-CODE-SHUFFLE-PARTITIONS: glue_shuffle_reduction_v1
    GLUE-CODE-SINGLE-PARTITION: glue_shuffle_reduction_v1
    GLUE-IS-CAPACITY-REVIEW: glue_interactive_capacity_reduction_v1
    SFN-STANDARD-TO-EXPRESS: sfn_standard_to_express_v1
    SM-CODE-CPU-ONLY-ON-GPU: sagemaker_gpu_to_cpu_instance_v1
    SM-CODE-NO-CHECKPOINT: sagemaker_managed_spot_training_v1
    SM-TRAINING-SPOT-CANDIDATE: sagemaker_managed_spot_training_v1
  deterministic_fields_are_immutable:
    - estimated_gain
    - difficulty_score
    - confidence
    - execution_priority
    - strategic_priority
  verdicts:
    - confirmed
    - rejected
    - needs_evidence
  remediation_families:
    - advisor_backlog
    - capacity_sizing
    - commitment_purchase
    - data_demand
    - driver_memory_cache
    - failure_waste
    - idle_capacity
    - incremental_state
    - inventory_integrity
    - lifecycle_cleanup
    - observability_gap
    - orchestration_waste
    - output_layout
    - read_pruning
    - result_reuse
    - row_level_processing
    - runtime_modality
    - schedule_frequency
    - shuffle_partitioning
    - silent_failure
    - storage_class
    - training_convergence
  documentation_domain: docs.aws.amazon.com
---

<!-- GERADO por scripts/generate_skill_registry.py a partir de docs/ai/. Não edite este arquivo: edite a fonte canônica e regenere. -->

# Julius — análise econômica de sinal

## purpose

Existe desperdício comprovado para o qual nenhuma fórmula fecha. Uma UDF Python
impede otimização do Spark — isso é fato do script. Quanto ela custa **neste** job
depende da cardinalidade, do plano, do que a UDF faz e de quantas vezes é chamada
por linha, e nenhuma métrica coletável responde.

Sem esta Skill o motor devolve `needs_evidence` e a conversa acaba: o sinal fica
sem ordem de grandeza, ao lado de outros catorze, e o que decide qual investigar
primeiro passa a ser nada.

O que sai daqui **não é economia**. É uma faixa com raciocínio explícito, que o
motor confere contra o baseline que ele mesmo resolve, e que nunca entra no total
oficial.

## trigger conditions

Todas, simultaneamente:

- o sinal tem veredito `confirmed`;
- o `rule_id` está na lista de elegíveis do pacote;
- o motor não oferece método de estimativa para aquele sinal — havendo método, o
  caminho é a proposta de cenário, não esta Skill.

## inputs

- o sinal confirmado, com `evidence_ref` e o artefato completo;
- o custo do ativo, que vem no pacote e **não** se propõe;
- o mecanismo de cobrança do serviço, do catálogo;
- métricas do ativo na janela: duração, execuções, falhas, capacidade.

## expected output

Um objeto `contextual_estimate` no veredito daquele sinal, com mecanismo de
cobrança, raciocínio, entradas nomeadas, faixa, premissas, plano de validação,
documentação oficial e evidência ausente.

## does

1. **Identifica o mecanismo de cobrança** pela chave do catálogo — não por
   descrição livre. É o que liga a faixa à unidade que a AWS cobra de fato.
2. **Escreve o raciocínio em uma linha** que outra pessoa consiga refazer.
3. **Nomeia as entradas** que usou. Cada chave é conferida contra o pacote.
4. **Monta a faixa** com mínimo, esperado e máximo, conservadora por construção.
5. **Declara como validar**, do jeito que sai da estimativa para a medição.

## does not

- não propõe baseline: o custo do ativo é resolvido pelo motor, e um baseline
  proposto pela análise é a forma mais direta de um número inventado ganhar
  aparência de conta feita;
- não propõe tarifa: preço vem da tabela versionada por região;
- não aplica percentual sem justificativa escrita;
- não produz faixa para `rule_id` fora da lista de elegíveis;
- não soma nada ao portfólio — este caminho é permanentemente `not_eligible`;
- não trata a faixa como economia, nem no texto que a acompanha;
- não estima onde falta baseline, mecanismo ou documentação: aí a resposta é
  dizer o que falta.

## rules

Valem as regras globais de `docs/ai/regras-globais.md`, e três consequências que
esta Skill torna concretas:

- **a faixa não pode passar do custo do ativo.** Economia maior que o baseline
  exigiria custo downstream comprovado, e comprová-lo é outro cálculo. O motor
  corta o excesso e registra que cortou;
- **mínimo faturável entra na conta.** 10 MB por query Athena, 128 KB por objeto
  em classe fria, 1 minuto por execução de Glue. Estimar sem eles promete
  economia que não acontece;
- **plano de validação vazio não é estimativa.** Se ninguém sabe como confirmar,
  é palpite, e o lugar dele é `missing_evidence`.

## evidence requirements

- o sinal precisa estar `confirmed`, com `evidence_ref` apontando o artefato;
- toda entrada citada em `inputs` precisa existir no pacote;
- ao menos uma referência em `https://docs.aws.amazon.com/` sustentando o
  **mecanismo** — documentação promocional não é tarifa e não serve aqui;
- o que faltou para a conta fechar vai em `missing_evidence`, nomeado.

## output contract

O objeto `contextual_estimate` no veredito do sinal. O motor recusa a faixa e a
rebaixa para `needs_evidence` quando: o mecanismo não corresponde ao `rule_id`; o
raciocínio está vazio; não há entradas nomeadas; não há plano de validação; não
há premissa; falta documentação oficial; ou o ativo não tem custo resolvível.

## escalation conditions

- o `rule_id` não está entre os elegíveis — não force a faixa, registre a lacuna
  em `uncovered_findings`;
- o custo do ativo não aparece no pacote;
- o mecanismo de cobrança do serviço não está no catálogo;
- a única faixa que se sustenta passa do custo do ativo — isso indica custo
  downstream, que é outra análise e outra evidência.

---

## Procedimento operacional — DEVIN CLI

Este é o único arquivo específico de host. Ele descreve **como** executar o
Julius numa sessão Devin; **o que** analisar está na Skill canônica, que é a
mesma para qualquer host.

### Instalação

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

### Identidade

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

### Coleta

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

### Pacote de análise

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

### Artefatos finais

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

### Critérios de conclusão

O validador já cobre a maior parte deles, e o que ele cobre não se repete aqui.
Restam três que nenhum teste verifica e que dependem de julgamento:

- o diagnóstico explica **esta** ocorrência, e não o padrão em geral;
- a justificativa de cada veredito se sustenta sozinha para quem não leu o script;
- a ordem de implementação é coerente com as dependências declaradas.
