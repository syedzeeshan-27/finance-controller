# Thin wrapper — the real script is scripts/repro.py (single source of truth).
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\repro.ps1
$ErrorActionPreference = "Stop"
python "$PSScriptRoot\repro.py" @args
exit $LASTEXITCODE
