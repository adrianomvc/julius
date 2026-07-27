#!/usr/bin/env bash
# Instala o Julius para o DEVIN CLI em Linux/macOS (e Git Bash no Windows).
#
# O que ele faz, nesta ordem:
#   1. resolve o repositório (o clone atual, ou clona em ~/.julius);
#   2. escolhe um Python >= 3.11 e cria a venv;
#   3. instala o pacote com os extras (`aws,dev`);
#   4. instala o lançador `julius` em ~/.local/bin;
#   5. instala a skill julius-aws-analysis no diretório de skills do DEVIN CLI;
#   6. registra os arquivos de configuração locais, sempre desabilitados;
#   7. valida: suíte de testes + smoke com os dados de exemplo.
#
# Nada aqui acessa a AWS nem envia e-mail: a coleta ao vivo depende de um
# `aws sso login` posterior, feito por uma pessoa na máquina de trabalho.
#
# Uso:
#   bash install/install.sh
#   curl -fsSL https://raw.githubusercontent.com/adrianomvc/julius/refs/heads/main/install/install.sh | bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/adrianomvc/julius.git"

REPO_URL="${JULIUS_REPO_URL:-$DEFAULT_REPO_URL}"
EXTRAS="${JULIUS_EXTRAS-aws,dev}"
SKIP_TESTS="${JULIUS_SKIP_TESTS:-0}"
SKIP_SMOKE="${JULIUS_SKIP_SMOKE:-0}"
SKIP_CONFIG="${JULIUS_SKIP_CONFIG:-0}"
SKIP_SKILL="${JULIUS_SKIP_SKILL:-0}"
SKIP_LAUNCHER="${JULIUS_SKIP_LAUNCHER:-0}"

START_TS="$(date +%s)"

info() { echo "[julius] $*"; }
warn() { echo "[julius] $*" >&2; }
die() { echo "[julius] $*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git é necessário e não está no PATH."

# ---------------------------------------------------------------------------
# 1. Origem: o clone onde este script está, ou um clone novo em ~/.julius.
#
# No Devin o repositório já está em ~/repos/julius e o setup roda de lá; clonar
# uma segunda cópia faria os testes rodarem contra o repositório errado. Por
# isso a instalação é *no lugar* quando o script vem de um clone, e só clona
# quando ele chega sozinho (`curl | bash`, onde não há BASH_SOURCE em disco).
# ---------------------------------------------------------------------------
SCRIPT_DIR=""
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
fi

is_julius_repo() {
  [ -f "$1/pyproject.toml" ] && grep -q '^name = "julius"' "$1/pyproject.toml"
}

LOCAL_REPO=""
if [ -n "$SCRIPT_DIR" ] && is_julius_repo "$(dirname "$SCRIPT_DIR")"; then
  LOCAL_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

INSTALL_DIR="${JULIUS_INSTALL_DIR:-${LOCAL_REPO:-$HOME/.julius}}"

if [ "$INSTALL_DIR" = "$LOCAL_REPO" ]; then
  info "Instalando no clone atual: $INSTALL_DIR"
elif is_julius_repo "$INSTALL_DIR"; then
  info "Atualizando a instalação existente em $INSTALL_DIR"
  if [ -d "$INSTALL_DIR/.git" ]; then
    # Atualização best-effort: um clone com alterações locais continua servindo,
    # e falhar aqui deixaria a máquina sem Julius por causa de um `git pull`.
    git -C "$INSTALL_DIR" fetch --quiet origin main 2>/dev/null &&
      git -C "$INSTALL_DIR" merge --quiet --ff-only origin/main 2>/dev/null ||
      warn "Não foi possível avançar para origin/main; seguindo com o conteúdo atual."
  fi
elif [ -e "$INSTALL_DIR" ]; then
  die "$INSTALL_DIR existe e não é um clone do Julius. Nada foi alterado; mova o diretório ou informe JULIUS_INSTALL_DIR."
else
  # Clone em duas etapas: um candidato validado antes de virar a instalação,
  # para um clone interrompido não deixar ~/.julius pela metade.
  CANDIDATE_DIR="${INSTALL_DIR}.candidate.$$"
  info "Clonando $REPO_URL em $CANDIDATE_DIR"
  git clone --quiet --branch main "$REPO_URL" "$CANDIDATE_DIR"
  if is_julius_repo "$CANDIDATE_DIR"; then
    mv "$CANDIDATE_DIR" "$INSTALL_DIR"
    info "Clonado em $INSTALL_DIR"
  else
    rm -rf "$CANDIDATE_DIR"
    die "O clone não parece ser o Julius; a instalação não foi alterada."
  fi
fi

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$INSTALL_DIR/pyproject.toml" | head -n1)"
info "Julius ${VERSION:-desconhecido}"

# ---------------------------------------------------------------------------
# 2. Python >= 3.11.
#
# `python3` no Ubuntu 22.04 — a imagem do Devin — é o 3.10, e o projeto usa
# tomllib e StrEnum, que são 3.11+. Procurar interpretadores por nome antes de
# desistir é o que faz a instalação funcionar sem mexer no `python3` do sistema.
# ---------------------------------------------------------------------------
python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

PYTHON_BIN=""
if [ -n "${JULIUS_PYTHON:-}" ]; then
  python_ok "$JULIUS_PYTHON" ||
    die "JULIUS_PYTHON=$JULIUS_PYTHON não é um Python 3.11+."
  PYTHON_BIN="$JULIUS_PYTHON"
else
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
  # Git Bash no Windows: os nomes acima costumam não existir, mas o launcher
  # oficial (`py`) resolve o interpretador real.
  if [ -z "$PYTHON_BIN" ] && command -v py >/dev/null 2>&1; then
    for launcher_arg in -3.14 -3.13 -3.12 -3.11 -3; do
      CANDIDATE_PATH="$(py "$launcher_arg" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
      if [ -n "$CANDIDATE_PATH" ] && python_ok "$CANDIDATE_PATH"; then
        PYTHON_BIN="$CANDIDATE_PATH"
        break
      fi
    done
  fi
fi

if [ -z "$PYTHON_BIN" ]; then
  warn "Nenhum Python 3.11+ encontrado no PATH."
  warn "Ubuntu 22.04 (imagem do Devin) traz o 3.10; instale um 3.11+ e rode de novo:"
  warn "  sudo add-apt-repository -y ppa:deadsnakes/ppa"
  warn "  sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv"
  warn "Ou aponte um interpretador existente: JULIUS_PYTHON=/caminho/python3.11 bash install/install.sh"
  exit 1
fi
info "Python: $PYTHON_BIN ($("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"

# ---------------------------------------------------------------------------
# 3. venv + pacote.
# ---------------------------------------------------------------------------
VENV_DIR="${JULIUS_VENV_DIR:-$INSTALL_DIR/.venv}"

venv_python() {
  for candidate in "$VENV_DIR/bin/python" "$VENV_DIR/Scripts/python.exe"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

VENV_PY="$(venv_python || true)"
if [ -n "$VENV_PY" ] && python_ok "$VENV_PY"; then
  info "Reaproveitando a venv em $VENV_DIR"
else
  if [ -e "$VENV_DIR" ]; then
    info "venv em $VENV_DIR está ausente ou é antiga demais; recriando."
    rm -rf "$VENV_DIR"
  fi
  info "Criando a venv em $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" ||
    die "Falha ao criar a venv. No Ubuntu instale o pacote correspondente (ex.: sudo apt-get install -y python3.11-venv)."
  VENV_PY="$(venv_python)" || die "venv criada sem interpretador em $VENV_DIR."
fi

# O alvo é `.` a partir do próprio diretório: um caminho absoluto do Git Bash
# (/d/Projetos/julius) não é um caminho que o Python do Windows entenda.
TARGET="."
[ -n "$EXTRAS" ] && TARGET=".[${EXTRAS}]"
info "Instalando o pacote (${EXTRAS:-sem extras}) — pode levar alguns minutos na primeira vez."
(cd "$INSTALL_DIR" && "$VENV_PY" -m pip install --quiet --disable-pip-version-check -e "$TARGET") ||
  die "pip install falhou. Verifique acesso à rede/registro PyPI e rode de novo."
info "Pacote instalado."

# ---------------------------------------------------------------------------
# 4. Lançador `julius` fora da venv.
# ---------------------------------------------------------------------------
# O lançador é gerado aqui, e não copiado de um segundo arquivo: os caminhos da
# instalação só existem neste ponto, e um template solto seria mais um script a
# manter em sincronia com este.
if [ "$SKIP_LAUNCHER" != "1" ]; then
  BIN_DIR="${JULIUS_BIN_DIR:-$HOME/.local/bin}"
  mkdir -p "$BIN_DIR"
  {
    # Só as duas primeiras linhas interpolam: o corpo vai literal, com os `$`
    # que o lançador precisa resolver na hora em que ele roda.
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      '# Gerado por install/install.sh — chama o CLI dentro da venv, de qualquer' \
      '# diretório, sem `activate`. Reinstale para mudar os caminhos abaixo.' \
      'set -euo pipefail' \
      "JULIUS_HOME=\"\${JULIUS_HOME:-$INSTALL_DIR}\"" \
      "JULIUS_VENV_DIR=\"\${JULIUS_VENV_DIR:-$VENV_DIR}\""
    cat <<'LAUNCHER'

# Windows/Git Bash usa Scripts/; POSIX usa bin/.
for candidate in "$JULIUS_VENV_DIR/bin/julius" "$JULIUS_VENV_DIR/Scripts/julius.exe"; do
  [ -x "$candidate" ] && exec "$candidate" "$@"
done

for candidate in "$JULIUS_VENV_DIR/bin/python" "$JULIUS_VENV_DIR/Scripts/python.exe"; do
  [ -x "$candidate" ] && exec "$candidate" -m julius.cli "$@"
done

echo "Julius não está instalado em $JULIUS_VENV_DIR." >&2
echo "Rode o instalador: bash $JULIUS_HOME/install/install.sh" >&2
exit 1
LAUNCHER
  } >"$BIN_DIR/julius"
  chmod +x "$BIN_DIR/julius"
  info "Lançador instalado em $BIN_DIR/julius"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR não está no PATH. Acrescente: export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

# ---------------------------------------------------------------------------
# 5. Skill do DEVIN CLI.
#
# Caminho documentado: ~/.config/devin/skills no POSIX e %APPDATA%\devin\skills
# no Windows (o Git Bash exporta APPDATA). O repositório mantém a fonte em
# .agents/skills/ e o AGENTS.md da raiz é lido pelo Devin sem cópia nenhuma.
# ---------------------------------------------------------------------------
if [ "$SKIP_SKILL" != "1" ]; then
  if [ -n "${APPDATA:-}" ]; then
    if command -v cygpath >/dev/null 2>&1; then
      APPDATA_UNIX="$(cygpath -u "$APPDATA")"
    else
      APPDATA_UNIX="$(printf '%s' "$APPDATA" | tr '\\' '/')"
    fi
    SKILLS_DIR="${JULIUS_SKILLS_DIR:-$APPDATA_UNIX/devin/skills}"
  else
    SKILLS_DIR="${JULIUS_SKILLS_DIR:-$HOME/.config/devin/skills}"
  fi

  SKILL_SOURCE="$INSTALL_DIR/.agents/skills/julius-aws-analysis/SKILL.md"
  if [ -f "$SKILL_SOURCE" ]; then
    SKILL_TARGET="$SKILLS_DIR/julius-aws-analysis"
    mkdir -p "$SKILL_TARGET"
    cp -f "$SKILL_SOURCE" "$SKILL_TARGET/SKILL.md"
    info "Skill instalada em $SKILL_TARGET/SKILL.md"
  else
    warn "Skill não encontrada em $SKILL_SOURCE; o CLI funciona, a skill não será listada."
  fi

  if command -v devin >/dev/null 2>&1; then
    # A verificação roda fora do repositório de propósito: dentro dele o Devin
    # já lista a skill a partir de .agents/skills, e um `grep` verde ali não
    # provaria que a cópia global — a que serve os outros repositórios —
    # funcionou.
    NEUTRAL_DIR="$(mktemp -d)"
    if (cd "$NEUTRAL_DIR" && devin skills list 2>&1) | grep -q "julius-aws-analysis"; then
      info "OK: julius-aws-analysis aparece em 'devin skills list' fora do repositório."
    else
      info "Skill copiada, mas não apareceu em 'devin skills list'. Confira 'devin skills paths'."
    fi
    rm -rf "$NEUTRAL_DIR"
  else
    info "DEVIN CLI não está no PATH; os arquivos estão instalados. Confirme depois com 'devin skills list'."
  fi
fi

# ---------------------------------------------------------------------------
# 6. Configuração local — sempre desabilitada, nunca sobrescrita.
#
# Os exemplos já vêm com `enabled: false` e e-mail em dry-run. Copiar o exemplo
# não habilita conta nenhuma: quem habilita é uma pessoa, editando o arquivo.
# ---------------------------------------------------------------------------
if [ "$SKIP_CONFIG" != "1" ]; then
  # Best-effort de ponta a ponta: a configuração é conveniência, e um HOME que
  # não aceita escrita não pode derrubar uma instalação que já está pronta.
  seed_config() {
    local example="$INSTALL_DIR/$1" target="$HOME/$2"
    [ -f "$example" ] || return 0
    if [ -f "$target" ]; then
      info "Configuração já existente, mantida: $target"
    elif mkdir -p "$HOME" 2>/dev/null && cp "$example" "$target" 2>/dev/null; then
      info "Configuração criada (desabilitada): $target"
    else
      warn "Não foi possível criar $target; copie $example manualmente."
    fi
  }
  seed_config ".julius-accounts.example.json" ".julius-accounts.json"
  seed_config ".julius-email.example.json" ".julius-email.json"
  seed_config ".julius-recipients.example.json" ".julius-recipients.json"
fi

# ---------------------------------------------------------------------------
# 7. Validação: testes e smoke com os dados de exemplo.
#
# A AWS CLI só é necessária para a coleta ao vivo, que não acontece aqui — a
# ausência dela é um aviso, não um erro, senão a instalação quebraria numa
# imagem limpa do Devin sem motivo.
# ---------------------------------------------------------------------------
if command -v aws >/dev/null 2>&1; then
  info "AWS CLI: $(aws --version 2>&1)"
else
  info "AWS CLI não encontrada. A análise com dados de exemplo funciona; para 'julius collect' instale a AWS CLI e rode 'aws sso login'."
fi

if [ "$SKIP_TESTS" != "1" ]; then
  if "$VENV_PY" -c 'import pytest' >/dev/null 2>&1; then
    info "Rodando a suíte de testes..."
    (cd "$INSTALL_DIR" && "$VENV_PY" -m pytest -q) ||
      die "A suíte de testes falhou. A instalação está no disco, mas não confie nela até isso passar."
  else
    info "pytest não instalado (extras sem 'dev'); testes ignorados."
  fi
else
  info "Testes ignorados (JULIUS_SKIP_TESTS=1)."
fi

if [ "$SKIP_SMOKE" != "1" ]; then
  SAMPLE="$INSTALL_DIR/data/sample/consumer-avi.json"
  if [ -f "$SAMPLE" ]; then
    SMOKE_DIR="$(mktemp -d)"
    trap 'rm -rf "$SMOKE_DIR"' EXIT
    info "Smoke test com os dados de exemplo..."
    # O `report` é o smoke que importa: é ele que carrega os templates e os
    # assets empacotados. Um `opportunities` verde não prova que o relatório
    # renderiza — e é justamente o template que some quando o empacotamento
    # regride.
    (cd "$INSTALL_DIR" && "$VENV_PY" -m julius.cli report \
      --input "$SAMPLE" \
      --output "$SMOKE_DIR/reports" \
      --store "$SMOKE_DIR/backlog.json" \
      --signal-ledger "$SMOKE_DIR/signals.json" \
      --history-db "$SMOKE_DIR/julius.duckdb" \
      --parquet-dir "$SMOKE_DIR/parquet" >/dev/null) ||
      die "O smoke test falhou: 'julius report' não gerou os artefatos."
    [ -s "$SMOKE_DIR/reports/report.html" ] ||
      die "O smoke test rodou, mas report.html saiu vazio ou não foi gerado."
    info "Smoke OK: report.html gerado a partir de data/sample/consumer-avi.json."
  else
    warn "Dados de exemplo ausentes em $SAMPLE; smoke ignorado."
  fi
else
  info "Smoke ignorado (JULIUS_SKIP_SMOKE=1)."
fi

ELAPSED=$(( $(date +%s) - START_TS ))
info "Pronto em ${ELAPSED}s. Instalação: $INSTALL_DIR"
info "Rode 'julius --help' ou peça a skill julius-aws-analysis no Devin."
