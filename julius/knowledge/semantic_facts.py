"""O que um veredito pode escrever de volta no inventário, e o que não pode.

Existe uma classe pequena de campos que nenhuma API da AWS responde porque a
resposta é propriedade da lógica de negócio. `StateMachine.idempotent` é o caso
exemplar e, hoje, o único: o modelo diz que ele fica `None` "até a análise
contextual julgar a ASL". A regra `SFN-STANDARD-TO-EXPRESS` exigia
`idempotent is True` para propor a migração, ninguém preenchia o campo, e a maior
economia unitária do Step Functions nunca disparava em conta real. A regra existia
inteira e estava desligada por um campo vazio.

Este catálogo é o que liga veredito a campo. Ele nasce com **uma** entrada, e isso
é resultado, não rascunho: varrer o modelo inteiro atrás de campos declarados como
não-coletáveis encontra exatamente um.

**Por que a lista não cresce por conveniência.** A tentação é catalogar todo campo
que uma regra usa como porta. `SageMakerJob.checkpoint_configured` parece um
candidato — a regra de spot depende dele. Mas ele **é coletável**: vem de
`CheckpointConfig` em `describe_training_job`. Deixar um veredito escrevê-lo
sobrescreveria fato medido com opinião, que é exatamente o que a regra dos campos
determinísticos imutáveis proíbe. `_alvo_e_coletado` recusa esse caso, e o teste
que o cobre existe para essa lição não precisar ser reaprendida.

**A direção do veredito importa e é fácil de inverter.** Um sinal confirmado
afirma a hipótese do sinal, e a hipótese de `SFN-STANDARD-TO-EXPRESS` é *esta
máquina deveria migrar*. Confirmar é dizer que a reexecução é tolerável — daí
`idempotent = True`. Ler ao contrário recomendaria Express para a máquina que
duplica cobrança, que é o dano que a pergunta existe para evitar.

**Só `confirmed` escreve.** `rejected` pode significar coisas diferentes —
"duplica cobrança" e "não vale o esforço" chegam com o mesmo rótulo. Derivar
`idempotent = False` de um "não" ambíguo seria inventar fato a partir de silêncio.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticFact:
    """Um campo que só a leitura responde, e o veredito que o preenche."""

    fact_type: str
    #: O sinal cuja confirmação afirma este fato.
    rule_id: str
    #: Coleção do inventário e atributo escritos quando o sinal é confirmado.
    collection: str
    attribute: str
    #: O valor escrito. Nunca numérico: número é campo determinístico.
    value_on_confirmed: bool
    #: Por que nenhuma API responde isto. Entrada sem esta frase não entra.
    why_not_collectable: str
    #: O que a confirmação afirma, em uma frase — para o leitor conferir a direção.
    means: str


CATALOG: tuple[SemanticFact, ...] = (
    SemanticFact(
        fact_type="at_least_once_safe",
        rule_id="SFN-STANDARD-TO-EXPRESS",
        collection="state_machines",
        attribute="idempotent",
        value_on_confirmed=True,
        why_not_collectable=(
            "tolerar semântica at-least-once é propriedade da lógica de negócio; "
            "nenhuma operação do Step Functions a expõe"
        ),
        means=(
            "a reexecução desta máquina é tolerável, logo ela pode migrar para "
            "Express"
        ),
    ),
)


def by_rule(rule_id: str) -> SemanticFact | None:
    return next((item for item in CATALOG if item.rule_id == rule_id), None)


def known_fact_types() -> tuple[str, ...]:
    return tuple(sorted(item.fact_type for item in CATALOG))


def apply_confirmed(account, decisions) -> list[str]:
    """Escreve no inventário o que já foi julgado. Devolve o que mudou.

    Três portas, e nenhuma é decorativa: veredito `confirmed`, `rule_id` no
    catálogo, e procedência declarada. Um veredito sem assinatura de evidência não
    pode ser invalidado depois — e um fato que não expira é pior que um fato
    ausente, porque continua valendo sobre um artefato que já mudou.
    """
    aplicados: list[str] = []
    for decision in decisions:
        if getattr(decision, "verdict", "") != "confirmed":
            continue
        fato = by_rule(str(getattr(decision, "rule_id", "")))
        if fato is None:
            continue
        if not str(getattr(decision, "evidence_hash", "") or ""):
            # Sem a assinatura do que sustentou o julgamento, o livro de sinais
            # não consegue reabrir a pergunta quando o artefato mudar.
            continue
        alvo = _resolver(account, fato, str(getattr(decision, "asset_name", "")))
        if alvo is None:
            continue
        if getattr(alvo, fato.attribute, None) is fato.value_on_confirmed:
            continue
        setattr(alvo, fato.attribute, fato.value_on_confirmed)
        aplicados.append(
            f"{getattr(alvo, 'name', '?')}: {fato.attribute}="
            f"{fato.value_on_confirmed} por veredito contextual ({fato.fact_type})"
        )
    return aplicados


def _resolver(account, fato: SemanticFact, asset_name: str):
    itens = getattr(account, fato.collection, None) or ()
    return next(
        (item for item in itens if getattr(item, "name", None) == asset_name), None
    )
