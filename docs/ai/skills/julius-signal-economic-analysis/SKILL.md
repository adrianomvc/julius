---
name: julius-signal-economic-analysis
description: Produz uma faixa de ordem de grandeza para sinal confirmado cujo desperdício é real e para o qual nenhuma fórmula do motor fecha, sempre separada da economia oficial.
trigger: Ativar quando um sinal confirmado tiver rule_id na lista de elegíveis a faixa contextual e o motor não tiver método de estimativa para ele.
sections_to_load:
  - does
  - does not
  - rules
  - evidence requirements
  - output contract
---

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
