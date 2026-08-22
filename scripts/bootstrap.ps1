# Crush-Sim bootstrap — Windows (PowerShell)
# Usage: scripts\bootstrap.ps1   (run from the repo root)
# Creates .venv, installs the package + dev deps (incl. Windows extras), runs `csim doctor`.
$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

$py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $py -c "import sys; assert sys.version_info >= (3, 11), f'Python 3.11+ required, got {sys.version}'"

if (-not (Test-Path ".venv")) {
    & $py -m venv .venv
}
& .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
# Windows machines get the CATIA COM extra (pywin32) and CAD extra (OCP) on top of dev deps.
pip install -e ".[dev,windows,cad]"
if ($LASTEXITCODE -ne 0) { pip install -e "." }

Write-Host ""
Write-Host "=== Environment self-check (Phase 0 Day-1 gate) ==="
csim doctor

Write-Host @"

Bootstrap complete.
  Activate the environment:   .\.venv\Scripts\Activate.ps1
  Run the smoke pipeline:     csim all --config configs/cases/lc2_default.yaml
  Install the solver:         scripts\install_openradioss.ps1   (see configs/solver.yaml)
  Fetch legacy reference:     scripts\fetch_legacy.ps1          (only needed to re-run FR-10 harvest)
"@
