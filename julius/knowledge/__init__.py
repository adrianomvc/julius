"""Conhecimento de domínio: o que o Julius sabe sobre AWS e sobre custo.

Três coisas, separadas de propósito:

- `pricing/` — tarifas e faixas de recuperação. **Premissas versionadas**, não
  fatos: trocar por preço de contrato é mudar um valor aqui, sem tocar em regra
  nem em coletor.
- `thresholds/` — os limiares que separam "normal" de "vale olhar".
- `rules/` — as regras por serviço, cada uma com o modelo financeiro ao lado.

A camada abaixo é a coleta, e a seta aponta só para lá: conhecimento lê o
inventário, nunca o contrário. Quando a coleta precisa de uma tabela daqui — a
classificação de `USAGE_TYPE` do Glue, por exemplo — ela a recebe como
parâmetro em vez de importá-la.
"""
