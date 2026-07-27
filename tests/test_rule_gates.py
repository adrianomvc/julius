"""Um campo que uma regra usa como porta precisa de alguém que o escreva.

Sete defeitos desta linha de trabalho tinham a mesma forma: um campo cujo valor
padrão significava alguma coisa, e ninguém preenchendo esse campo.
`idle_shutdown_min = 0` queria dizer "shutdown desligado" e virava falso
positivo; `idempotent = None` matava a regra de Express; `touches_90d = 0`
afirmava "ninguém toca esta tabela" e reservava dinheiro.

Nenhum foi pego por teste. Todos foram encontrados lendo código, um a um,
porque o dataset de exemplo é escrito à mão com tudo preenchido — ele descreve a
conta que gostaríamos de ter coletado, não a que a coleta produz.

Estas duas asserções são mecânicas de propósito. Elas não sabem o que cada
regra faz; sabem só que afirmar sobre um campo exige que alguém o preencha, e
que medida ausente não pode ter default que passe por medida.
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields as dataclass_fields
from pathlib import Path

from julius.collection import models

RAIZ = Path(__file__).resolve().parents[1] / "julius"
REGRAS = RAIZ / "knowledge" / "rules"
COLETA = RAIZ / "collection"

#: Campos lidos por regra que nenhum coletor preenche, com o motivo de estarem
#: aqui. A exceção documentada é o registro de uma decisão; o teste falhando é
#: o que força tomá-la.
SEM_ESCRITOR_CONHECIDOS = {
    # Conhecimento de negócio, não propriedade que a AWS exponha. Vale como
    # afirmação explícita quando o dataset a traz, e na ausência dela a
    # oportunidade nasce bloqueada com o sinal GLUE-FLEX-TOLERANCE.
    "time_sensitive",
    # Mesma natureza: só a análise contextual, lendo a ASL, pode afirmar.
    "idempotent",
    # Depende de inspecionar a fonte; hoje mantém GLUE-BOOKMARK-OFF bloqueada
    # com motivo explícito, que é o comportamento correto enquanto falta.
    "incremental_source_evidence",
    # Usados só em texto e num filtro; coletá-los é trabalho registrado e
    # adiado, não esquecido.
    "storage_bytes",
    "temporary",
}

#: Default numérico aceito, e o critério é um só: **a ausência falha para o
#: silêncio, não para a afirmação**.
#:
#: `touches_90d = 0` reprovava porque a regra dispara em `<= 0` — não medir
#: virava "ninguém usa esta tabela", com dinheiro. `invocations_per_month = 0`
#: reprovava pelo mesmo motivo. Já `observed_runs = 0` passa porque toda regra
#: que o lê exige `>=` algum mínimo: não medir faz a regra calar.
#:
#: Ao acrescentar um campo aqui, a pergunta a responder no comentário é qual
#: lado o zero puxa. Se puxa para o achado, não é exceção — é bug.
DEFAULT_LEGITIMO = {
    # Ausência de conceito, não de medida: workgroup serverless não tem nós.
    ("RedshiftCluster", "node_count"),
    ("RedshiftCluster", "base_rpu"),
    # Contadores de evento. As regras exigem mínimo (`>= min_runs`,
    # `>= high_job_frequency`), então zero silencia.
    ("GlueJob", "runs_in_window"),
    ("GlueJob", "observed_runs"),
    ("GlueJob", "overlapping_runs_in_window"),
    ("GlueJob", "retry_runs_in_window"),
    ("StateMachine", "sampled_executions"),
    ("StateMachine", "observed_runs"),
    ("StateMachine", "executions_per_month"),
    ("AthenaQuery", "observed_runs"),
    ("AthenaQuery", "executions_per_month"),
    ("InteractiveSession", "observed_runs"),
    ("GlueCrawler", "runs_in_window"),
    ("GlueCrawler", "failures_in_window"),
    ("DataBrewJob", "runs_in_window"),
    ("DataBrewJob", "failures_in_window"),
    # Tempo somado das execuções da janela; os gates exigem `> 0` ou um piso.
    ("GlueJob", "active_seconds_window"),
    ("GlueJob", "overlap_seconds_window"),
    # Consumo que multiplica o baseline: zero produz baseline zero, e nenhum
    # achado reivindica economia sobre nada.
    ("GlueCrawler", "dpu_hours_window"),
    ("DataBrewJob", "estimated_node_hours_window"),
    # Ociosidade medida. As regras exigem um mínimo de horas ociosas, então não
    # medir deixa o app e a sessão de fora em vez de acusá-los.
    ("SageMakerApp", "idle_hours_per_day"),
    ("InteractiveSession", "idle_hours_per_day"),
    # Dias com datapoint; a regra de ociosidade exige `observed_days > 0`.
    ("RedshiftCluster", "observed_days"),
}

#: Nome de campo que sugere medida, e não configuração declarada.
_MEDICAO = re.compile(
    r"(_window|_per_day|_per_month|touches|invocations|observed|"
    r"idle_hours|connections|queries_)"
)


def _fonte(diretorio: Path) -> str:
    return "".join(
        caminho.read_text(encoding="utf-8") for caminho in diretorio.rglob("*.py")
    )


def _campos_usados_como_porta() -> set[str]:
    """Atributos lidos dentro de condicional nos módulos de regra.

    Aproximação deliberada: qualquer atributo lido num `if`, `while` ou
    expressão booleana conta como porta. Erra para o lado de cobrar demais, e é
    isso que se quer de uma rede de segurança.
    """
    encontrados: set[str] = set()
    for caminho in REGRAS.rglob("*.py"):
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.If | ast.While | ast.BoolOp | ast.Compare):
                continue
            for interno in ast.walk(no):
                if isinstance(interno, ast.Attribute) and isinstance(
                    interno.value, ast.Name
                ):
                    encontrados.add(interno.attr)
    return encontrados


def _campos_de_modelo() -> dict[str, set[str]]:
    """Campo → classes de inventário que o declaram."""
    por_campo: dict[str, set[str]] = {}
    for nome in dir(models):
        classe = getattr(models, nome)
        if not (isinstance(classe, type) and hasattr(classe, "__dataclass_fields__")):
            continue
        for campo in dataclass_fields(classe):
            por_campo.setdefault(campo.name, set()).add(nome)
    return por_campo


def test_every_field_a_rule_gates_on_has_someone_writing_it():
    """Uma regra que lê o que ninguém escreve nunca dispara, ou sempre dispara."""
    escritores = _fonte(COLETA)
    modelos = _campos_de_modelo()
    portas = _campos_usados_como_porta()

    orfaos = set()
    for campo in portas & set(modelos):
        if campo in SEM_ESCRITOR_CONHECIDOS or campo.startswith("_"):
            continue
        escrito = re.search(rf"\b{campo}\s*=[^=]", escritores) or re.search(
            rf'"{campo}"', escritores
        )
        if not escrito:
            orfaos.add(f"{'/'.join(sorted(modelos[campo]))}.{campo}")

    assert not orfaos, (
        "campo usado como porta sem ninguém que o escreva — a regra está morta "
        f"ou sempre ligada: {sorted(orfaos)}. Colete o campo, mova o julgamento "
        "para um sinal, ou registre a exceção em SEM_ESCRITOR_CONHECIDOS."
    )


def test_a_measurement_field_cannot_default_to_a_meaningful_value():
    """`0` não pode querer dizer ao mesmo tempo "medi e deu zero" e "não medi"."""
    portas = _campos_usados_como_porta()

    suspeitos = []
    for nome in dir(models):
        classe = getattr(models, nome)
        if not (isinstance(classe, type) and hasattr(classe, "__dataclass_fields__")):
            continue
        for campo in dataclass_fields(classe):
            if campo.name not in portas or (nome, campo.name) in DEFAULT_LEGITIMO:
                continue
            if not _MEDICAO.search(campo.name):
                continue
            anotacao = str(campo.type)
            if "None" in anotacao or "Optional" in anotacao:
                continue
            if campo.default in (0, 0.0, False):
                suspeitos.append(f"{nome}.{campo.name} = {campo.default!r}")

    assert not suspeitos, (
        "campo de medição com default que significa algo: "
        f"{sorted(suspeitos)}. Use `| None` para separar ausência de zero, ou "
        "registre a exceção em DEFAULT_LEGITIMO com o motivo."
    )


def test_the_exception_lists_stay_honest():
    """Exceção que não corresponde a campo real vira desculpa esquecida."""
    modelos = _campos_de_modelo()

    fantasmas = SEM_ESCRITOR_CONHECIDOS - set(modelos)
    assert not fantasmas, f"exceção para campo inexistente: {sorted(fantasmas)}"

    for classe, campo in DEFAULT_LEGITIMO:
        assert classe in modelos.get(campo, set()), (
            f"exceção para {classe}.{campo}, que não existe mais"
        )
