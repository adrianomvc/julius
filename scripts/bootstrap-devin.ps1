param(
    [string]$Python = "python",
    [string]$VenvDir = ".venv"
)

$ErrorActionPreference = "Stop"

& $Python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
& $Python -m venv $VenvDir
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
& $VenvPython -m pip install -e ".[aws,dev]"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "AWS CLI não encontrado. Instale/configure antes da coleta ao vivo."
}

aws --version
& $VenvPython -m pytest -q
& $VenvPython -m julius.cli opportunities --input "data/sample/consumer-avi.json"

Write-Host "Julius pronto. Inicie a conversa no Devin e peça a Skill julius-aws-analysis."
