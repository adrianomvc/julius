#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${JULIUS_VENV_DIR:-.venv}"

"${PYTHON_BIN}" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e '.[aws,dev]'

if ! command -v aws >/dev/null 2>&1; then
  echo "AWS CLI não encontrado. Instale/configure antes da coleta ao vivo." >&2
  exit 2
fi

aws --version
"${VENV_DIR}/bin/python" -m pytest -q
"${VENV_DIR}/bin/python" -m julius.cli opportunities \
  --input data/sample/consumer-avi.json

echo "Julius pronto. Inicie a conversa no Devin e peça a Skill julius-aws-analysis."
