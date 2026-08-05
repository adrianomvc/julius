"""Monta os artefatos de Skill a partir da fonte canônica e do motor.

A direção importava e estava invertida. `.agents/skills/julius-aws-analysis/SKILL.md`
era a fonte: um arquivo em inglês, com o procedimento de sessão do Devin no meio,
que o instalador copiava para o diretório de skills do Devin. Trocar de host
significava copiar aquele arquivo — com o procedimento do outro host dentro dele.
E as mesmas regras existiam de novo, com outro texto, em `guardrails.py`, sem
nenhum teste comparando os dois.

Aqui a fonte é `docs/ai/`, em português e sem host, e `.agents/skills/` passa a
ser artefato. A montagem tem três partes:

1. **prosa canônica** — vem do markdown e não é gerada; ninguém quer editar
   parágrafo dentro de um `.py` — um arquivo "gerado" cujo texto mora no
   gerador é uma segunda fonte de verdade escondida onde ninguém procura;
2. **campos derivados do motor** — `rule_id`, métodos de estimativa, campos
   determinísticos, versão do prompt. Escritos à mão eles divergem: foi assim que
   dois métodos de estimativa ficaram fora do briefing por meses;
3. **bloco do host** — o único pedaço específico de Devin, Claude ou do que vier.

O que amarra tudo é `check()`: se o artefato no disco não for exatamente o que
esta função produz, o teste falha. Editar o artefato à mão deixa de ser possível
sem que apareça.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
CANONICO = RAIZ / "docs" / "ai"

#: Host → onde o artefato daquele host é instalado no repositório.
#:
#: Acrescentar um host é acrescentar uma linha aqui e um bloco em
#: `docs/ai/hosts/`. Nada de conteúdo se duplica: o corpo canônico é o mesmo em
#: todo artefato.
#:
#: Hoje há um host só, por decisão de produto — a análise roda no Devin. O
#: mecanismo continua multi-host porque é ele que impede o artefato de virar
#: fonte, e `tests/test_host_agnostic.py` o exercita com um host sintético em vez
#: de um segundo artefato de verdade. Com um host real só, um teste que comparasse
#: artefatos seria trivialmente verdadeiro e não provaria nada.
HOSTS = {
    "devin": RAIZ / ".agents" / "skills",
}

_AVISO = (
    "<!-- GERADO por scripts/generate_skill_registry.py a partir de docs/ai/. "
    "Não edite este arquivo: edite a fonte canônica e regenere. -->"
)


class SkillSourceError(ValueError):
    """A fonte canônica não respeita o contrato de Skill."""


@dataclass(frozen=True)
class SkillSource:
    """Uma Skill canônica: frontmatter escrito à mão e corpo em markdown."""

    name: str
    path: Path
    frontmatter: dict[str, object]
    body: str


#: Chaves que a fonte canônica precisa declarar, e que ninguém gera por ela.
FRONTMATTER_OBRIGATORIO = ("name", "description", "trigger", "sections_to_load")

#: Seções que todo corpo de Skill precisa ter. O resto é opcional e específico.
SECOES_OBRIGATORIAS = (
    "purpose",
    "inputs",
    "expected output",
    "does",
    "does not",
    "rules",
    "output contract",
)


def _parse_frontmatter(texto: str, origem: Path) -> tuple[dict[str, object], str]:
    """Frontmatter YAML mínimo: escalares e listas de um nível.

    Deliberadamente sem PyYAML. O contrato é pequeno e conhecido, e uma
    dependência nova para ler quatro chaves seria custo sem contrapartida.
    """
    linhas = texto.splitlines()
    if not linhas or linhas[0].strip() != "---":
        raise SkillSourceError(f"Skill sem frontmatter: {origem}")
    fim = next(
        (i for i, linha in enumerate(linhas[1:], start=1) if linha.strip() == "---"),
        None,
    )
    if fim is None:
        raise SkillSourceError(f"frontmatter não fechado: {origem}")

    dados: dict[str, object] = {}
    chave_atual: str | None = None
    for bruta in linhas[1:fim]:
        if not bruta.strip():
            continue
        if bruta.startswith("  - "):
            if chave_atual is None:
                raise SkillSourceError(f"item de lista sem chave em {origem}")
            acumulado = dados.get(chave_atual)
            if not isinstance(acumulado, list):
                acumulado = []
                dados[chave_atual] = acumulado
            acumulado.append(bruta[4:].strip())
            continue
        casa = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", bruta)
        if not casa:
            raise SkillSourceError(f"linha de frontmatter não suportada em {origem}: {bruta!r}")
        chave_atual = casa.group(1)
        valor = casa.group(2).strip()
        dados[chave_atual] = valor if valor else []
    return dados, "\n".join(linhas[fim + 1 :]).lstrip("\n")


def load_skill(path: Path) -> SkillSource:
    """Lê e valida uma Skill canônica."""
    frontmatter, corpo = _parse_frontmatter(path.read_text(encoding="utf-8"), path)
    faltando = [k for k in FRONTMATTER_OBRIGATORIO if k not in frontmatter]
    if faltando:
        raise SkillSourceError(f"{path}: frontmatter sem {', '.join(faltando)}")
    if not isinstance(frontmatter.get("sections_to_load"), list) or not frontmatter["sections_to_load"]:
        raise SkillSourceError(f"{path}: sections_to_load precisa ser lista não vazia")
    for secao in SECOES_OBRIGATORIAS:
        if not re.search(rf"(?im)^##\s+{re.escape(secao)}\s*$", corpo):
            raise SkillSourceError(f"{path}: falta a seção obrigatória '{secao}'")
    return SkillSource(
        name=str(frontmatter["name"]),
        path=path,
        frontmatter=frontmatter,
        body=corpo,
    )


def load_skills() -> list[SkillSource]:
    return [load_skill(p) for p in sorted((CANONICO / "skills").glob("*/SKILL.md"))]


#: Os três casos que toda Skill precisa ter antes de existir. É o mínimo do
#: Uma Skill nasce de lacuna observada, com dois ou três casos concretos que
#: servem de critério de aceitação — e para aqui de propósito: o resto entra
#: quando houver falha real que o justifique, não porque a lista ficaria mais
#: bonita completa.
CASOS_OBRIGATORIOS = ("confirmed", "rejected", "needs_evidence")


@dataclass(frozen=True)
class Eval:
    """Um caso de aceitação, e o teste que o cobra.

    `enforced_by` é o que separa isto de prosa. Um eval que descrevesse o
    comportamento esperado sem apontar quem o verifica seria uma segunda fonte de
    verdade sobre o mesmo assunto — e a que envelhece primeiro, porque nada
    quebra quando ela fica errada.
    """

    skill: str
    case: str
    rule_id: str
    enforced_by: str
    path: Path


def load_evals(skill_name: str) -> list[Eval]:
    pasta = CANONICO / "evals" / skill_name
    encontrados = []
    for caminho in sorted(pasta.glob("*.md")):
        dados, _ = _parse_frontmatter(caminho.read_text(encoding="utf-8"), caminho)
        faltando = [
            chave
            for chave in ("skill", "case", "rule_id", "enforced_by")
            if chave not in dados
        ]
        if faltando:
            raise SkillSourceError(f"{caminho}: eval sem {', '.join(faltando)}")
        encontrados.append(
            Eval(
                skill=str(dados["skill"]),
                case=str(dados["case"]),
                rule_id=str(dados["rule_id"]),
                enforced_by=str(dados["enforced_by"]),
                path=caminho,
            )
        )
    return encontrados


def eval_problems() -> list[str]:
    """Skill sem os casos obrigatórios, ou eval mal formado."""
    achados: list[str] = []
    for skill in load_skills():
        try:
            evals = load_evals(skill.name)
        except SkillSourceError as exc:
            achados.append(str(exc))
            continue
        if not evals:
            achados.append(f"Skill sem evals: {skill.name}")
            continue
        casos = {item.case for item in evals}
        faltando = [caso for caso in CASOS_OBRIGATORIOS if caso not in casos]
        if faltando:
            achados.append(
                f"{skill.name}: falta eval para {', '.join(faltando)}"
            )
        divergentes = [
            item.path.name for item in evals if item.skill != skill.name
        ]
        if divergentes:
            achados.append(
                f"{skill.name}: eval declarando outra Skill: {divergentes}"
            )
    return achados


def contract_digest() -> str:
    """A impressão digital do que a análise recebe e do que ela precisa devolver.

    `PROMPT_VERSION` é uma versão só, e qualquer mudança a sobe — decisão do dono
    do produto, e é a que faz o rastro funcionar. Todo veredito grava a versão do
    briefing que o produziu (`SignalLedger.record_verdicts` → coluna
    `prompt_version` em DuckDB); com versões separadas para Skill, playbook,
    contrato e schema, reproduzir um julgamento exigiria quatro números.

    Só que "qualquer mudança sobe a versão" é promessa que ninguém cumpre de
    memória. Este dígito cobre a diferença: ele resume regras, corpo canônico,
    playbooks, métodos permitidos, schema de saída **e a prosa do briefing**, e o
    teste compara com o valor congelado. Mudar conteúdo sem subir a versão falha,
    e a mensagem diz o que fazer.

    A prosa entrou por último e era o furo maior. O dígito cobria o dado
    estruturado e não o texto que diz o que fazer com ele — `DETERMINISTIC`, a
    divisão por grau de certeza, as quatro tarefas. Tudo isso é instrução, vai em
    todo pacote, e podia ser reescrito sem que a versão se mexesse.
    """
    import hashlib
    import json

    from julius.analysis.guardrails import RULES, canonical_briefing
    from julius.analysis.playbook import select
    from julius.analysis.response_validator import ANALYSIS_OUTPUT_SCHEMA

    material = {
        "rules": list(RULES),
        "skills": {skill.name: skill.body for skill in load_skills()},
        "playbooks": {asset: list(perguntas) for asset, perguntas in select(None)},
        "engine": {
            chave: valor
            for chave, valor in engine_fields().items()
            if chave != "prompt_version"
        },
        "schema": ANALYSIS_OUTPUT_SCHEMA,
        # A prosa que apresenta tudo acima. Sem ela, o dígito cobria os dados e
        # não o texto que diz o que fazer com eles — e reescrever a divisão de
        # trabalho não subia versão nenhuma. Cobre também `DETERMINISTIC`, que
        # nunca foi campo do motor e mesmo assim vai em todo briefing.
        "briefing": canonical_briefing(),
    }
    bruto = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:16]


def engine_fields() -> dict[str, object]:
    """O que o motor sabe e a Skill não pode contradizer.

    Cada chave aqui tem dono no código. Nenhuma é opinião do autor da Skill, e é
    por isso que são geradas em vez de escritas.
    """
    from julius.analysis.context_builder import DETERMINISTIC_FIELDS
    from julius.analysis.guardrails import PROMPT_VERSION
    from julius.knowledge.contextual_estimation import allowed_methods
    from julius.knowledge.generative_estimation import eligible_rule_ids
    from julius.knowledge.remediation import FAMILIES

    metodos = allowed_methods()
    return {
        "prompt_version": PROMPT_VERSION,
        "allowed_estimation_methods": sorted(set(metodos.values())),
        "estimation_methods_by_rule": dict(sorted(metodos.items())),
        "deterministic_fields_are_immutable": list(DETERMINISTIC_FIELDS),
        "verdicts": ["confirmed", "rejected", "needs_evidence"],
        # O validador recusa `remediation_family` fora desta lista, então ela é
        # parte do contrato: a análise precisa saber quais nomes existem, e mudar
        # o catálogo precisa subir a versão como qualquer outra mudança de regra.
        "remediation_families": sorted(FAMILIES),
        # A lista que o briefing anuncia em `_generative_eligibility`. Sem ela
        # aqui, acrescentar uma regra elegível mudava o que a análise é instruída
        # a fazer sem mudar a versão — e `prompt_version`, gravado em todo
        # veredito, deixava de identificar a instrução que o produziu.
        "contextual_range_rules": list(eligible_rule_ids()),
        "documentation_domain": "docs.aws.amazon.com",
    }


def _render_yaml(dados: dict[str, object], indent: str = "") -> list[str]:
    linhas = []
    for chave, valor in dados.items():
        if isinstance(valor, list):
            linhas.append(f"{indent}{chave}:")
            linhas.extend(f"{indent}  - {item}" for item in valor)
        elif isinstance(valor, dict):
            linhas.append(f"{indent}{chave}:")
            linhas.extend(_render_yaml(valor, indent + "  "))
        else:
            linhas.append(f"{indent}{chave}: {valor}")
    return linhas


def render_host_artifact(skill: SkillSource, host: str) -> str:
    """Frontmatter escrito + campos do motor + corpo canônico + bloco do host."""
    bloco = CANONICO / "hosts" / f"{host}.md"
    if not bloco.is_file():
        raise SkillSourceError(f"bloco de host ausente: {bloco}")

    # `name` e `description` são as duas chaves que todo host entende. O resto —
    # a convenção de contrato que o Julius adota, mais o que vem do motor — vai
    # sob `metadata`, que é onde um host guarda o que não é do schema dele.
    #
    # Não é detalhe cosmético: `trigger` e `sections_to_load` soltos no topo são
    # atributo desconhecido para o schema de skills do VS Code, e um artefato que
    # nasce inválido no host errado é precisamente o que ter um gerador evita. A
    # fonte canônica segue plana e legível; quem se adapta é a saída.
    metadados: dict[str, object] = {
        chave: valor
        for chave, valor in skill.frontmatter.items()
        if chave not in ("name", "description")
    }
    metadados.update(engine_fields())

    partes = ["---"]
    partes += _render_yaml(
        {chave: skill.frontmatter[chave] for chave in ("name", "description")}
    )
    partes.append("metadata:")
    partes.append("  # Gerado a partir de docs/ai/ e do motor — não edite.")
    partes += _render_yaml(metadados, indent="  ")
    partes.append("---")
    partes.append("")
    partes.append(_AVISO)
    partes.append("")
    partes.append(skill.body.rstrip())
    partes.append("")
    partes.append("---")
    partes.append("")
    # O procedimento do host entra rebaixado um nível: dentro do artefato ele é
    # uma seção da Skill, não um documento paralelo.
    corpo_host = bloco.read_text(encoding="utf-8").rstrip()
    partes.append(re.sub(r"(?m)^(#+) ", r"\1# ", corpo_host))
    return "\n".join(partes) + "\n"


def render_registry() -> str:
    """A tabela do registry. Só tabela — a prosa mora no markdown canônico."""
    from julius.analysis.context_builder import DETERMINISTIC_FIELDS
    from julius.analysis.guardrails import PROMPT_VERSION
    from julius.knowledge.contextual_estimation import allowed_methods

    metodos = sorted(set(allowed_methods().values()))
    linhas = [
        "# Registry de Skills do Julius",
        "",
        "> Gerado por `scripts/generate_skill_registry.py` a partir de `docs/ai/`.",
        "> Não edite a tabela à mão: `--check` falha quando ela diverge.",
        "",
        "## Skills",
        "",
        "| Skill | Fonte | Trigger | Seções a carregar |",
        "|---|---|---|---|",
    ]
    for skill in load_skills():
        origem = skill.path.relative_to(RAIZ).as_posix()
        secoes = ", ".join(skill.frontmatter.get("sections_to_load", []))  # type: ignore[arg-type]
        linhas.append(
            f"| `{skill.name}` | [{origem}]({origem}) | {skill.frontmatter['trigger']} | {secoes} |"
        )
    linhas += [
        "",
        "## Campos derivados do motor",
        "",
        "Estes valores não são escritos à mão em lugar nenhum: vêm do código que os",
        "aplica, e são injetados no frontmatter de cada artefato.",
        "",
        "| Campo | Origem | Valor |",
        "|---|---|---|",
        f"| `prompt_version` | `analysis.guardrails.PROMPT_VERSION` | `{PROMPT_VERSION}` |",
        "| `allowed_estimation_methods` | `knowledge.contextual_estimation.allowed_methods()` | "
        + ", ".join(f"`{item}`" for item in metodos)
        + " |",
        "| `deterministic_fields_are_immutable` | `analysis.context_builder.DETERMINISTIC_FIELDS` | "
        + ", ".join(f"`{item}`" for item in DETERMINISTIC_FIELDS)
        + " |",
        "",
        "## Artefatos gerados por host",
        "",
        "| Host | Artefato |",
        "|---|---|",
    ]
    for host, destino in sorted(HOSTS.items()):
        for skill in load_skills():
            alvo = (destino / skill.name / "SKILL.md").relative_to(RAIZ).as_posix()
            linhas.append(f"| `{host}` | [{alvo}]({alvo}) |")
    return "\n".join(linhas) + "\n"


def expected_files() -> dict[Path, str]:
    """Todo arquivo gerado e o conteúdo exato que ele deveria ter."""
    saida: dict[Path, str] = {CANONICO / "registry.md": render_registry()}
    for host, destino in HOSTS.items():
        for skill in load_skills():
            saida[destino / skill.name / "SKILL.md"] = render_host_artifact(skill, host)
    return saida


def write_all() -> list[Path]:
    escritos = []
    for caminho, conteudo in expected_files().items():
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(conteudo, encoding="utf-8", newline="\n")
        escritos.append(caminho)
    return sorted(escritos)


def check() -> list[str]:
    """O que está fora do lugar. Lista vazia significa sem drift."""
    problemas = eval_problems()
    for caminho, esperado in expected_files().items():
        relativo = caminho.relative_to(RAIZ).as_posix()
        if not caminho.is_file():
            problemas.append(f"artefato ausente: {relativo}")
            continue
        if caminho.read_text(encoding="utf-8") != esperado:
            problemas.append(f"artefato desatualizado: {relativo}")
    return problemas
