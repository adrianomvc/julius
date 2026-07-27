# Install — Julius para o DEVIN CLI

Um único script, `install/install.sh`, prepara a máquina inteira:

1. **Repositório** — usa o clone de onde o script foi chamado; quando ele chega
   sozinho (`curl | bash`), clona em `~/.julius`.
2. **Python 3.11+** — procura `python3.14 … python3.11`, `python3`, `python` e,
   no Git Bash, o launcher `py`. O `python3` do Ubuntu 22.04 é o 3.10 e o
   projeto usa `tomllib` e `StrEnum`; sem um 3.11+ o script para com a instrução
   de instalação, não com um traceback.
3. **venv + pacote** — cria a `.venv` e instala `-e .[aws,dev]`. Reaproveita uma
   venv existente quando ela já é 3.11+.
4. **Lançador** — gera `~/.local/bin/julius`, que chama o CLI dentro da venv de
   qualquer diretório, sem `activate`. Ele é gerado com os caminhos já
   resolvidos, e não copiado de um template: um segundo script solto só teria
   como sair de sincronia com este.
5. **Skill** — copia `.agents/skills/julius-aws-analysis/SKILL.md` para o
   diretório global de skills do DEVIN CLI (`~/.config/devin/skills` no POSIX,
   `%APPDATA%\devin\skills` no Windows), que é o que torna
   `/julius-aws-analysis` disponível **fora** deste repositório. Dentro dele o
   Devin já enxerga a skill em `.agents/skills`, e o `AGENTS.md` da raiz é lido
   sem cópia nenhuma.
6. **Configuração** — cria `~/.julius-accounts.json`, `~/.julius-email.json` e
   `~/.julius-recipients.json` a partir dos exemplos, **todos desabilitados** e
   com e-mail em `dry-run`. Arquivo existente nunca é sobrescrito.
7. **Validação** — roda a suíte de testes e um smoke `julius report` sobre
   `data/sample/consumer-avi.json`, exigindo um `report.html` não vazio.

Nada disso acessa a AWS nem envia e-mail. A coleta ao vivo depende de um
`aws sso login` posterior, feito por uma pessoa na máquina de trabalho; a AWS
CLI ausente vira aviso, não erro, porque a imagem limpa do Devin não a tem e a
análise com dados de exemplo funciona sem ela.

```bash
bash install/install.sh

# Sem clone prévio
curl -fsSL https://raw.githubusercontent.com/adrianomvc/julius/refs/heads/main/install/install.sh | bash
```

## Devin: onde cada passo entra no assistente de setup

O Devin roda Ubuntu 22.04, clona o repositório em `~/repos/julius` e **cada
comando do assistente tem 5 minutos de limite**. Por isso o install e os testes
entram em campos diferentes:

| Campo do assistente | Comando |
| --- | --- |
| Install Dependencies | `sudo apt-get install -y python3.11 python3.11-venv \|\| true`<br>`JULIUS_SKIP_TESTS=1 bash install/install.sh` |
| Maintain Dependencies | `JULIUS_SKIP_TESTS=1 JULIUS_SKIP_SMOKE=1 bash install/install.sh` |
| Set up Lint | `.venv/bin/ruff check . && .venv/bin/mypy julius` |
| Set up Tests | `.venv/bin/python -m pytest -q` |
| Run Local App | `.venv/bin/python -m julius.cli report --input data/sample/consumer-avi.json` |

`JULIUS_SKIP_TESTS=1` no install existe só para caber no limite de 5 minutos: a
suíte continua obrigatória, no campo que o Devin roda antes de cada commit. Se o
`apt-get` falhar por falta de sudo, instale o 3.11 no snapshot da máquina uma
vez e o install passa a encontrá-lo sozinho.

A re-execução é idempotente de ponta a ponta: venv reaproveitada, skill
recopiada, configuração preservada.

## Variáveis

| Variável | Default | Para quê |
| --- | --- | --- |
| `JULIUS_PYTHON` | (descoberta automática) | Fixa o interpretador 3.11+ |
| `JULIUS_INSTALL_DIR` | clone atual, ou `~/.julius` | Onde o Julius mora |
| `JULIUS_VENV_DIR` | `$INSTALL_DIR/.venv` | Onde a venv mora |
| `JULIUS_BIN_DIR` | `~/.local/bin` | Onde o lançador é instalado |
| `JULIUS_SKILLS_DIR` | skills do DEVIN CLI | Destino da skill |
| `JULIUS_REPO_URL` | GitHub público | Mirror interno para o clone |
| `JULIUS_EXTRAS` | `aws,dev` | Extras do pacote (vazio = nenhum) |
| `JULIUS_SKIP_TESTS` | `0` | Pula a suíte |
| `JULIUS_SKIP_SMOKE` | `0` | Pula o smoke do relatório |
| `JULIUS_SKIP_CONFIG` | `0` | Não cria os arquivos `~/.julius-*.json` |
| `JULIUS_SKIP_SKILL` | `0` | Não instala a skill |
| `JULIUS_SKIP_LAUNCHER` | `0` | Não instala `~/.local/bin/julius` |

## Quando algo falha

- **`Nenhum Python 3.11+ encontrado`** — instale o 3.11 (a mensagem traz os
  comandos) ou aponte `JULIUS_PYTHON`.
- **`Falha ao criar a venv`** — no Ubuntu o módulo `venv` vem em pacote
  separado: `sudo apt-get install -y python3.11-venv`.
- **`pip install falhou`** — acesso ao PyPI. Num ambiente com mirror interno,
  configure o `pip.conf`/`~/.pypirc` da máquina antes.
- **`A suíte de testes falhou`** — a instalação está no disco, mas não é
  confiável: o script sai com erro de propósito.
- **`report.html saiu vazio`** — quase sempre empacotamento: o template ou o
  asset deixou de ser incluído em `[tool.setuptools.package-data]`.
- **`julius: command not found`** — `~/.local/bin` fora do `PATH`; o próprio
  script avisa e mostra o `export` a acrescentar.
