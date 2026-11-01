Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Create .venv and install requirements.txt first. See README.md.'
}
& '.\.venv\Scripts\python.exe' -m luminext --host 127.0.0.1 --port 8000
