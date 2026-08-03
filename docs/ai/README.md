# Camada de IA do Julius — fonte canônica

Tudo que a análise contextual precisa saber mora aqui, em português e sem
depender de host nenhum. `.agents/skills/` é **artefato gerado** a partir deste
diretório; editá-lo à mão falha nos testes.

## Por onde entrar

| Arquivo | O que é |
|---|---|
| [`regras-globais.md`](regras-globais.md) | As doze regras que valem para qualquer provedor e qualquer Skill, cada uma com quem a cobra |
| [`precedencia.md`](precedencia.md) | Ordem de resolução quando duas orientações se contradizem, e por que conteúdo externo nunca sobe |
| [`skills/`](skills/) | As Skills canônicas, uma pasta por Skill |
| [`hosts/`](hosts/) | Procedimento operacional por host — o único conteúdo específico |
| [`registry.md`](registry.md) | Gerado: quais Skills existem, seus gatilhos e o que vem do motor |

## Como mudar alguma coisa

```bash
# 1. edite a fonte em docs/ai/
# 2. regenere os artefatos
python scripts/generate_skill_registry.py
# 3. confirme que não sobrou divergência
python scripts/generate_skill_registry.py --check
```

O passo 3 roda nos testes (`tests/test_skill_registry_drift.py`), então esquecer o
passo 2 quebra o build em vez de produzir um artefato desatualizado em silêncio.

## O que é escrito e o que é gerado

A separação é **por campo**, não por arquivo.

**Escrito à mão, aqui:** toda a prosa — `purpose`, `does`, `does not`, `rules`,
`evidence requirements` — mais `name`, `description`, `trigger` e
`sections_to_load`.

**Gerado do motor:** `prompt_version`, `allowed_estimation_methods`,
`estimation_methods_by_rule`, `deterministic_fields_are_immutable`, `verdicts` e
`documentation_domain`. Nenhum deles é opinião de quem escreve a Skill: cada um
tem dono no código, e escrevê-los à mão foi o que deixou dois métodos de
estimativa fora do briefing por meses.

O gerador monta; ele não guarda texto. Um teste recusa prosa da Skill dentro do
`.py` que a monta — a alternativa é o arquivo virar "gerado" enquanto o conteúdo
migra para dentro do gerador, que é uma segunda fonte de verdade escondida onde
ninguém procura.
