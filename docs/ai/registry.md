# Registry de Skills do Julius

> Gerado por `scripts/generate_skill_registry.py` a partir de `docs/ai/`.
> Não edite a tabela à mão: `--check` falha quando ela diverge.

## Skills

| Skill | Fonte | Trigger | Seções a carregar |
|---|---|---|---|
| `julius-aws-analysis` | [docs/ai/skills/julius-aws-analysis/SKILL.md](docs/ai/skills/julius-aws-analysis/SKILL.md) | Ativar quando for pedida análise de custo ou governança de uma conta AWS com o Julius, ou quando existir um pacote de análise contextual a responder. | does, does not, rules, evidence requirements, output contract |
| `julius-signal-economic-analysis` | [docs/ai/skills/julius-signal-economic-analysis/SKILL.md](docs/ai/skills/julius-signal-economic-analysis/SKILL.md) | Ativar quando um sinal confirmado tiver rule_id na lista de elegíveis a faixa contextual e o motor não tiver método de estimativa para ele. | does, does not, rules, evidence requirements, output contract |

## Campos derivados do motor

Estes valores não são escritos à mão em lugar nenhum: vêm do código que os
aplica, e são injetados no frontmatter de cada artefato.

| Campo | Origem | Valor |
|---|---|---|
| `prompt_version` | `analysis.guardrails.PROMPT_VERSION` | `2.0.0` |
| `allowed_estimation_methods` | `knowledge.contextual_estimation.allowed_methods()` | `glue_interactive_capacity_reduction_v1`, `glue_shuffle_reduction_v1`, `sagemaker_gpu_to_cpu_instance_v1`, `sagemaker_managed_spot_training_v1`, `sfn_standard_to_express_v1` |
| `deterministic_fields_are_immutable` | `analysis.context_builder.DETERMINISTIC_FIELDS` | `estimated_gain`, `difficulty_score`, `confidence`, `execution_priority`, `strategic_priority` |

## Artefatos gerados por host

| Host | Artefato |
|---|---|
| `claude` | [.claude/skills/julius-aws-analysis/SKILL.md](.claude/skills/julius-aws-analysis/SKILL.md) |
| `claude` | [.claude/skills/julius-signal-economic-analysis/SKILL.md](.claude/skills/julius-signal-economic-analysis/SKILL.md) |
| `devin` | [.agents/skills/julius-aws-analysis/SKILL.md](.agents/skills/julius-aws-analysis/SKILL.md) |
| `devin` | [.agents/skills/julius-signal-economic-analysis/SKILL.md](.agents/skills/julius-signal-economic-analysis/SKILL.md) |
