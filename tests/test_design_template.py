"""O desenho é do designer, e precisa continuar sendo.

Um relatório desenhado degrada de um jeito previsível: alguém precisa de um
campo novo, edita o HTML no repositório "só desta vez", e na entrega seguinte o
arquivo do designer e o do código já não são o mesmo documento. A partir daí não
existe mais como aceitar uma v2 sem merge manual.

Estes testes fecham as duas portas por onde isso entra:

- o sha256 do `.dc.html` — se alguém editar o desenho aqui, falha e diz que o
  lugar de mudar é no editor;
- o contrato lido **do próprio arquivo** — se a v2 trouxer um campo novo, falha
  em vez de renderizar um buraco em silêncio.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from julius.pipeline import analyze
from julius.reporting import design, design_view, renderer

SAMPLE = "data/sample/consumer-avi.json"

#: sha256 dos arquivos como vieram do pacote do designer.
ORIGINAIS = {
    design.SCREEN: "d9d1ba041854a4fb4d730b4cb14f4e987d4e9ceb24a5546acb8ef21b7f1a5c6a",
    design.PRINT: "c27551066298025b5d164f2150744a54b90529ac9b72756ebcf6b63252d6252e",
}


def _vm():
    return analyze(SAMPLE).vm


def _contexto():
    return design_view.build(_vm(), version="0.0-teste")


def _resolve(contexto: dict, caminho: str):
    """Segue `a.b.c` no contexto; `KeyError` diz qual elo faltou."""
    atual = contexto
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            raise KeyError(caminho)
        atual = atual[parte]
    return atual


@pytest.mark.parametrize("nome", [design.SCREEN, design.PRINT])
def test_the_design_file_is_the_designers_file(nome):
    conteudo = (design.DESIGN_DIR / nome).read_bytes()
    atual = hashlib.sha256(conteudo).hexdigest()

    assert atual == ORIGINAIS[nome], (
        f"{nome} foi editado no repositório. O desenho se mantém trocando o "
        f"arquivo por uma versão nova do designer, não editando esta cópia. "
        f"Se a troca é intencional, atualize o sha em ORIGINAIS."
    )


@pytest.mark.parametrize("nome", [design.SCREEN, design.PRINT])
def test_every_field_the_design_asks_for_has_a_value(nome):
    """O contrato sai do arquivo, não de uma lista escrita à mão.

    É o que faz uma v2 com campo novo falhar aqui em vez de renderizar vazio.
    """
    fonte = (design.DESIGN_DIR / nome).read_text(encoding="utf-8")
    variaveis, lacos = design.data_contract(fonte)
    contexto = _contexto()

    # Dentro de um `<sc-for … as="o">`, `o.title` se resolve no item, não na
    # raiz. E há laço aninhado — `o.evidence` roda dentro de `recsPrimary` —,
    # então a lista de um laço pode estar sob o alias de outro. Os laços vêm na
    # ordem do arquivo, e o de fora sempre vem antes.
    aliases: dict[str, object] = {}
    for lista, alias in lacos:
        raiz = lista.split(".")[0]
        if raiz in aliases:
            externo = aliases[raiz]
            itens = externo.get(lista.split(".", 1)[1]) if externo else None
        else:
            itens = _resolve(contexto, lista)
        aliases[alias] = itens[0] if itens else None

    faltando = []
    for nome_var in sorted(variaveis):
        raiz = nome_var.split(".")[0]
        if raiz in aliases:
            item = aliases[raiz]
            if item is None:
                continue  # lista vazia neste dataset; nada a cobrar
            campo = nome_var.split(".", 1)[1] if "." in nome_var else None
            # O alias de um laço sobre lista de texto (`o.evidence` → `e`) não
            # tem campo: `{{ e }}` já é o valor.
            if campo is not None and (not isinstance(item, dict) or campo not in item):
                faltando.append(nome_var)
            continue
        try:
            _resolve(contexto, nome_var)
        except KeyError:
            faltando.append(nome_var)

    assert not faltando, (
        f"o desenho pede campos que o adaptador não entrega: {faltando}. "
        f"Preencha em design_view.build."
    )


def test_no_placeholder_survives_rendering():
    html = renderer.render_html(_vm())

    assert not re.findall(r"\{\{[^}]*\}\}", html)
    assert "<sc-for" not in html and "<sc-if" not in html
    assert "support.js" not in html, "runtime do editor não vai na entrega"
    assert "x-dc" not in html, "invólucro do editor não vai na entrega"


def test_the_page_carries_its_own_assets():
    """Um arquivo que o analista reenvia por e-mail e continua abrindo."""
    html = renderer.render_html(_vm())

    assert 'src="assets/' not in html, "referência relativa quebra ao reenviar"
    assert "data:image/png;base64," in html, "o avatar precisa estar embutido"


def test_the_css_the_design_passes_inline_is_not_escaped():
    """Os chips são CSS pronto em `style="{{ … }}"` — escapado, viram texto."""
    html = renderer.render_html(_vm())

    assert 'style="font-size:10.5px;font-weight:700' in html
    assert not re.findall(r'style="[^"]*&#39;', html)


def test_account_data_is_still_escaped():
    """O `Markup` dos chips não pode ter desligado o autoescape do resto."""
    vm = _vm()
    vm.table[0].title = "<script>alert(1)</script>"

    html = renderer.render_html(vm)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_every_finding_lands_in_exactly_one_of_the_two_blocks():
    vm = _vm()
    contexto = design_view.build(vm, version="t")

    primarias = [o["id"] for o in contexto["recsPrimary"]]
    resto = [o["id"] for o in contexto["recsMore"]]

    assert len(primarias + resto) == len(set(primarias + resto))
    assert set(primarias + resto) == {o.id for o in vm.table}


def test_the_focus_is_never_hidden_behind_ver_mais():
    """A lista é ordenada por prioridade; o foco, por economia. Um item de
    foco em quinto lugar caía no bloco recolhido."""
    contexto = _contexto()

    primarias = {o["id"] for o in contexto["recsPrimary"]}
    foco = {o["id"] for o in contexto["focus"]}

    assert foco <= primarias, "recomendação em foco escondida no 'ver mais'"


def test_the_resource_identifier_is_never_empty_and_never_invented():
    contexto = _contexto()

    for item in contexto["recsPrimary"] + contexto["recsMore"]:
        assert item["arn"], f"{item['id']} sem identificador de recurso"
        if item["arn"].startswith("arn:"):
            # Um ARN só é útil se a AWS o reconhece: 6 campos, conta e região
            # preenchidas.
            partes = item["arn"].split(":")
            assert len(partes) >= 6, item["arn"]
            assert partes[3] and partes[4], f"ARN sem região/conta: {item['arn']}"


def test_every_dollar_of_saving_lands_on_a_service_line():
    """A contradição que o desenho novo põe na primeira seção.

    `service_of` cobria 4 dos 12 tipos de ativo que o catálogo produz, e o
    efeito era US$ 1.253,62 de US$ 1.557,01 aparecendo como "—" na tabela de
    serviços — ao lado, na mesma página, de uma recomendação de Redshift de
    US$ 988 que reivindicava boa parte disso. Enquanto a tabela vier do mesmo
    conjunto de achados, os dois lados têm que somar igual.
    """
    analysis = analyze(SAMPLE)
    vm = analysis.vm

    por_servico = sum(
        _valor(s["saving_fmt"]) for s in vm.services if s["name"] != "Outros"
    )
    dos_achados = sum(o.monthly for o in vm.table)

    assert abs(por_servico - dos_achados) < 0.02, (
        f"a tabela de serviços atribui US$ {por_servico:.2f} e as recomendações "
        f"somam US$ {dos_achados:.2f}. A diferença é economia que o relatório "
        f"promete sem dizer de onde sai."
    )


def _valor(formatado: str) -> float:
    """`US$ 1.253,62` → `1253.62`; `—` → `0`."""
    limpo = formatado.replace("US$", "").replace("R$", "").strip()
    if not limpo or limpo == "—":
        return 0.0
    return float(limpo.replace(".", "").replace(",", "."))
