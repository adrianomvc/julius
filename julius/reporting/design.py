"""O relatório desenhado, adotado sem que o desenho possa derivar.

O arquivo em `templates/design/` vem do editor do designer e entra aqui **byte
a byte como veio**. Nada no repositório o edita: quando chega uma versão nova,
troca-se o arquivo e pronto. É a diferença entre um desenho que se mantém e um
que se degrada — o segundo acontece quando alguém precisa refazer a tradução à
mão a cada entrega, e aproveita para "só ajustar" uma margem.

O que este módulo faz é traduzir, na carga, o pouco que o Jinja não executa:

- `<sc-for list="{{ x }}" as="y">…</sc-for>` → `{% for y in x %}…{% endfor %}`
- `<sc-if value="{{ x }}">…</sc-if>` → `{% if x %}…{% endif %}`

O `{{ campo }}` do editor já é sintaxe Jinja e passa intocado — foi o que
tornou esta adoção barata.

O invólucro do editor (`<x-dc>`, `<helmet>`, `support.js`) também sai na carga.
`support.js` é o runtime do editor, não acompanha o relatório entregue, e
deixá-lo referenciado renderiza uma página quebrada fora do editor.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

DESIGN_DIR = Path(__file__).parent / "templates" / "design"

SCREEN = "Relatorio Julius.dc.html"

#: `<sc-for list="{{ lista }}" as="alias" …>` — atributos extras do editor
#: (`hint-placeholder-count`) são ignorados.
_FOR_ABRE = re.compile(
    r"<sc-for\s+list=\"\{\{\s*([\w.]+)\s*\}\}\"\s+as=\"(\w+)\"[^>]*>"
)
_FOR_FECHA = re.compile(r"</sc-for>")

#: `<sc-if value="{{ campo }}" …>`
_IF_ABRE = re.compile(r"<sc-if\s+value=\"\{\{\s*([\w.]+)\s*\}\}\"[^>]*>")
_IF_FECHA = re.compile(r"</sc-if>")

#: O invólucro do editor, que não faz parte do desenho entregue.
_INVOLUCRO = (
    re.compile(r"</?x-dc>\s*"),
    re.compile(r"</?helmet>\s*"),
    re.compile(r"<script src=\"\./support\.js\"></script>\s*"),
    re.compile(r"<script src=\"\./doc-page\.js\"></script>\s*"),
    # O editor anexa os próprios props no fim do arquivo. É estado de edição,
    # não conteúdo, e carrega um JSON grande com a pré-visualização.
    re.compile(r"<script type=\"text/x-dc\"[^>]*>.*?</script>\s*", re.S),
)

#: O editor usa `style-hover` para pré-visualizar hover; o navegador ignora.
#: Sai porque atributo inválido em HTML entregue é sujeira, não desenho.
_STYLE_HOVER = re.compile(r'\s*style-hover="[^"]*"')


def to_jinja(source: str) -> str:
    """Traduz a sintaxe do editor para Jinja, sem tocar em mais nada."""
    out = _FOR_ABRE.sub(r"{% for \2 in \1 %}", source)
    out = _FOR_FECHA.sub("{% endfor %}", out)
    out = _IF_ABRE.sub(r"{% if \1 %}", out)
    out = _IF_FECHA.sub("{% endif %}", out)
    for padrao in _INVOLUCRO:
        out = padrao.sub("", out)
    return _STYLE_HOVER.sub("", out)


def load(name: str = SCREEN) -> str:
    """Lê o desenho e devolve o template Jinja equivalente."""
    return to_jinja((DESIGN_DIR / name).read_text(encoding="utf-8"))


def data_contract(source: str) -> tuple[set[str], list[tuple[str, str]]]:
    """O que o desenho exige de quem o alimenta.

    Devolve `(variáveis, laços)` lidos do próprio arquivo. É assim que uma
    versão nova do designer, com um campo a mais, falha no teste em vez de
    renderizar um buraco em silêncio.
    """
    variaveis = {
        nome
        for nome in re.findall(r"\{\{\s*([\w.]+)\s*\}\}", source)
        if nome not in {"false", "true"}
    }
    lacos = [
        (lista, alias)
        for lista, alias in re.findall(
            r"<sc-for\s+list=\"\{\{\s*([\w.]+)\s*\}\}\"\s+as=\"(\w+)\"", source
        )
    ]
    return variaveis, lacos


# ---------------------------------------------------------------------------
# Assets
#
# O relatório é entregue como um arquivo só, que o analista abre de onde salvou
# e reenvia por e-mail. Referência relativa a `assets/` quebra no primeiro
# reencaminhamento, e referência externa quebra em rede fechada — que é a rede
# de quem opera conta de dados. Então tudo que a página precisa vai dentro dela.
# ---------------------------------------------------------------------------

_AVATAR = re.compile(r'src="assets/julius-avatar\.png"')

#: Os três `<link>` do Google Fonts, na ordem em que o designer os escreveu.
_GOOGLE_FONTS = re.compile(
    r'<link rel="preconnect" href="https://fonts\.googleapis\.com">\s*'
    r'<link rel="preconnect" href="https://fonts\.gstatic\.com"[^>]*>\s*'
    r'<link href="https://fonts\.googleapis\.com/css2\?[^"]*"[^>]*>'
)

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"

#: `IBMPlexSans-600.woff2` → família `IBM Plex Sans`, peso 600.
_ARQUIVO_FONTE = re.compile(r"^([A-Za-z]+)-(\d{3})\.woff2$")

#: Como quebrar `IBMPlexSans` no nome que o CSS do desenho usa.
_FAMILIAS = {
    "IBMPlexSans": "IBM Plex Sans",
    "IBMPlexMono": "IBM Plex Mono",
    "SpaceGrotesk": "Space Grotesk",
}


def font_face_css(diretorio: Path = FONTS_DIR) -> str:
    """`@font-face` em base64 para os `.woff2` presentes — ou `""` se não houver.

    Devolver vazio é a resposta honesta quando os arquivos não estão lá: o
    `<link>` do designer permanece, e o CSS dele já declara `system-ui` como
    fallback. O contrário — emitir `@font-face` apontando para nada e remover o
    `<link>` — trocaria uma dependência externa por uma página sem tipografia.
    """
    if not diretorio.is_dir():
        return ""

    regras = []
    for arquivo in sorted(diretorio.glob("*.woff2")):
        casa = _ARQUIVO_FONTE.match(arquivo.name)
        if casa is None:
            continue
        familia = _FAMILIAS.get(casa.group(1))
        if not familia:
            continue
        dados = base64.b64encode(arquivo.read_bytes()).decode("ascii")
        regras.append(
            f"@font-face{{font-family:'{familia}';font-style:normal;"
            f"font-weight:{casa.group(2)};font-display:swap;"
            f"src:url(data:font/woff2;base64,{dados}) format('woff2');}}"
        )
    return f"<style>{''.join(regras)}</style>" if regras else ""


def inline_assets(html: str, *, avatar: str, fonts: str = "") -> str:
    """Troca as referências externas pelo conteúdo embutido."""
    if avatar:
        html = _AVATAR.sub(f'src="{avatar}"', html)
    if fonts:
        html = _GOOGLE_FONTS.sub(fonts, html)
    return html
