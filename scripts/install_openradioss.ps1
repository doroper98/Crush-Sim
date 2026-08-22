# Best-effort helper: download an official OpenRadioss release into tools\openradioss\ (git-ignored).
# The version tag is pinned in configs\solver.yaml. Per SPEC §9, installation and run
# flags must follow the official OpenRadioss README — verify after download:
#   https://github.com/OpenRadioss/OpenRadioss/releases
# Usage: scripts\install_openradioss.ps1 [-Tag <release-tag>]
param([string]$Tag = "")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $Tag) {
    $Tag = python -c "import yaml, pathlib; cfg = yaml.safe_load(pathlib.Path('configs/solver.yaml').read_text()); print(cfg.get('version_tag') or cfg.get('tag') or '')"
}
if (-not $Tag) {
    Write-Error "No release tag given and none pinned in configs\solver.yaml. Pick one from https://github.com/OpenRadioss/OpenRadioss/releases and pin it."
}

$Asset = "OpenRadioss_win64.zip"
$Url = "https://github.com/OpenRadioss/OpenRadioss/releases/download/$Tag/$Asset"
$Dest = "tools\openradioss"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

Write-Host "Downloading $Url ..."
try {
    Invoke-WebRequest -Uri $Url -OutFile (Join-Path $Dest $Asset)
    Expand-Archive -Path (Join-Path $Dest $Asset) -DestinationPath $Dest -Force
    Remove-Item (Join-Path $Dest $Asset)
    Write-Host "Extracted to $Dest\. Now:"
    Write-Host "  1. Follow the README inside the extracted archive for env setup (paths, OMP threads)."
    Write-Host "  2. Point configs\solver.yaml starter/engine paths at the extracted executables."
    Write-Host "  3. Re-run: csim doctor"
} catch {
    Write-Warning "Download failed — the asset name may differ for tag $Tag."
    Write-Warning "Download manually from https://github.com/OpenRadioss/OpenRadioss/releases/tag/$Tag"
    Write-Warning "and extract into $Dest\, then update configs\solver.yaml."
    exit 1
}
