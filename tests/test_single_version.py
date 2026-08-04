"""Uma versão só, e qualquer mudança de conteúdo a sobe.

`PROMPT_VERSION` não versiona o prompt: versiona o **contrato inteiro** que a
análise recebe e devolve — regras, corpo da Skill, playbooks, métodos permitidos
e schema de saída. Decisão do dono do produto, e é a que faz o rastro funcionar.

O motivo está em `SignalLedger.record_verdicts`, que grava a versão em todo
veredito, e em `state/history.py`, onde `prompt_version` é coluna `NOT NULL`. Sem
ela não dá para dizer qual pergunta produziu qual julgamento — e comparar a
precisão de dois briefings seria comparar duas perguntas diferentes sem saber.
Com versões separadas para Skill, playbook, contrato e schema, reproduzir um
julgamento exigiria quatro números em vez de um.

"Qualquer mudança sobe a versão" é promessa que ninguém cumpre de memória. O
dígito congelado abaixo é o que cobra: mudar conteúdo sem subir a versão falha
aqui, com a instrução do que fazer.
"""

from __future__ import annotations

from julius.analysis.guardrails import PROMPT_VERSION
from julius.analysis.skill_registry import contract_digest

#: Sobe junto com `PROMPT_VERSION`, nunca sozinho. Se este teste falhar, o
#: conteúdo do contrato mudou: suba `PROMPT_VERSION` e cole o dígito novo aqui.
#: 2.1.0 — expansão aditiva dos métodos de estimativa: `glue_shuffle_reduction_v1`
#: passou a responder `GLUE-CODE-SHUFFLE-PARTITIONS` e `GLUE-CODE-SINGLE-PARTITION`,
#: e seis regras entraram na faixa contextual.
#:
#: 2.2.0 — `remediation_family` entrou no veredito, opcional e validada contra o
#: catálogo, e a lista de famílias entrou nos campos do motor. Duas expansões
#: aditivas seguidas: nada saiu do contrato, nenhuma proibição mudou, e um veredito
#: dado em 2.0.0 continua legível em 2.2.0.
VERSAO_CONGELADA = "2.2.0"
DIGEST_CONGELADO = "f2ddf6120aaf479e"


def test_the_contract_did_not_change_without_a_version_bump():
    """A trava. Falhar aqui é o sistema funcionando, não quebrando."""
    atual = contract_digest()

    assert atual == DIGEST_CONGELADO, (
        f"o contrato da análise mudou (digest {atual}, esperado "
        f"{DIGEST_CONGELADO}).\n"
        "Regras, corpo de Skill, playbook, método permitido ou schema de saída "
        "foram alterados. Suba PROMPT_VERSION em julius/analysis/guardrails.py e "
        "atualize VERSAO_CONGELADA e DIGEST_CONGELADO neste arquivo — os três "
        "andam juntos, sempre."
    )


def test_the_frozen_version_matches_the_engine():
    """Congelar o dígito e esquecer a versão deixaria a dupla sem sentido."""
    assert PROMPT_VERSION == VERSAO_CONGELADA


def test_the_digest_actually_covers_the_contract():
    """Dígito que não muda com o conteúdo seria decoração.

    A contraprova roda sobre uma cópia das regras: se acrescentar uma regra não
    mudasse o dígito, o teste acima passaria para sempre.
    """
    from unittest.mock import patch

    from julius.analysis import guardrails

    original = contract_digest()
    with patch.object(guardrails, "RULES", (*guardrails.RULES, "regra nova")):
        alterado = contract_digest()

    assert alterado != original, (
        "acrescentar uma regra precisa mudar o dígito, ou ele não cobre nada"
    )


def test_the_version_reaches_every_place_that_records_it():
    """A versão precisa chegar onde o rastro é gravado, não só ao briefing."""
    from julius.analysis.context_builder import DETERMINISTIC_FIELDS  # noqa: F401
    from julius.analysis.skill_registry import engine_fields

    assert engine_fields()["prompt_version"] == PROMPT_VERSION

    from pathlib import Path

    historico = Path("julius/state/history.py").read_text(encoding="utf-8")
    assert "prompt_version VARCHAR NOT NULL" in historico, (
        "o histórico precisa exigir a versão; sem ela o veredito perde a origem"
    )
