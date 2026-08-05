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
#:
#: 3.0.0 — `julius-signal-economic-analysis` deixou de existir; o corpo dela virou
#: a seção `## contextual range` de `julius-aws-analysis`. Major porque um artefato
#: nomeado sumiu, e não porque o conteúdo mudou — ele foi preservado inteiro. Que
#: a Skill removida nunca tenha sido instalada (`install/install.sh` publica só a
#: que fica) é o motivo da fusão, não licença para tratá-la como aditiva: esta
#: versão é o rastro que liga cada veredito ao contrato que o produziu.
#: 3.1.0 — a família `table_format` entrou no catálogo de remediação, que é campo
#: do motor desde 2.2.0 e por isso parte do contrato. Aditiva: nenhuma família
#: saiu, e um veredito que declarou família em 3.0.0 continua válido.
#: 3.2.0 — `GLUE-CODE-PUSHDOWN` e `GLUE-CODE-FULL-OVERWRITE` passaram a aceitar
#: faixa contextual, e a lista de elegíveis entrou nos campos do motor. A segunda
#: parte é a que importa: o briefing anunciava essa lista desde sempre e o digest
#: não a cobria, então acrescentar uma regra mudava o que a análise é instruída a
#: fazer sem mudar a versão — e `prompt_version`, gravado em todo veredito,
#: deixava de identificar a instrução que o produziu. Aditiva.
#: 3.2.1 — patch, e é o primeiro. O conteúdo do contrato **não mudou**: a prosa do
#: briefing entrou no dígito, que até aqui cobria só dado estruturado. Um veredito
#: dado em 3.2.0 leu exatamente o mesmo texto que um dado em 3.2.1 — subir minor
#: diria que a análise foi instruída de outro jeito, e ela não foi. Patch é
#: "a trava passou a cobrir mais, o contrato continua o mesmo".
#: 3.3.0 — a Skill passou a nomear `remediation_family` e `suspected_injections`.
#: Os dois já existiam no schema e no validador, e o briefing já pedia o primeiro;
#: o que faltava era a Skill convidar. Campo opcional que ninguém pede não se
#: preenche, e `remediation_family` é o que diz que dois sinais são a mesma
#: correção. Aditiva: nada saiu, nenhuma proibição mudou.
VERSAO_CONGELADA = "3.3.0"
DIGEST_CONGELADO = "00277af69e4b71e9"


def test_the_contract_did_not_change_without_a_version_bump():
    """A trava. Falhar aqui é o sistema funcionando, não quebrando."""
    atual = contract_digest()

    assert atual == DIGEST_CONGELADO, (
        f"o contrato da análise mudou (digest {atual}, esperado "
        f"{DIGEST_CONGELADO}).\n"
        "Regras, corpo de Skill, playbook, método permitido, schema de saída ou "
        "a prosa do briefing foram alterados. Suba PROMPT_VERSION em "
        "julius/analysis/guardrails.py e "
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


def test_the_prose_counts_as_contract_too():
    """O texto que apresenta as regras é instrução tanto quanto elas.

    `DETERMINISTIC` é o caso que denunciou o furo: vai em todo briefing, diz o que
    a análise não deve refazer, e nunca foi campo do motor. Mudá-la reescrevia a
    divisão de trabalho sem mudar o dígito.
    """
    from unittest.mock import patch

    from julius.analysis import guardrails

    original = contract_digest()
    with patch.object(
        guardrails, "DETERMINISTIC", (*guardrails.DETERMINISTIC, "coisa nova")
    ):
        assert contract_digest() != original, (
            "mudar o que o briefing diz que já está decidido precisa mudar o dígito"
        )


def test_rewriting_the_tasks_changes_the_digest():
    """A contraprova direta: trocar quatro tarefas por outra coisa é mudança."""
    from unittest.mock import patch

    from julius.analysis import guardrails

    original = contract_digest()
    with patch.object(
        guardrails, "canonical_briefing", lambda: "faça o que quiser"
    ):
        assert contract_digest() != original


def test_bumping_the_version_alone_does_not_move_the_digest():
    """A exclusão é de propósito, e sem teste ela parece descuido.

    `PROMPT_VERSION` está na primeira linha do briefing. Se entrasse no dígito, o
    dígito mudaria toda vez que a versão subisse — inclusive quando subiu por
    outra causa —, e ele deixaria de responder "o conteúdo mudou?".
    """
    from unittest.mock import patch

    from julius.analysis import guardrails

    original = contract_digest()
    with patch.object(guardrails, "PROMPT_VERSION", "99.0.0"):
        assert contract_digest() == original


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
