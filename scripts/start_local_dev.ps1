param(
    [ValidateRange(1, 65535)][int]$Port = 8080,
    [string]$StateDirectory = "runtime\local-development"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Create the project .venv and install .[api,dev] first; see docs/LOCAL_DEVELOPMENT.md.'
}

& $Python -m linguistic_oj.local_dev --root $ProjectRoot --port $Port --state-dir $StateDirectory
exit $LASTEXITCODE
