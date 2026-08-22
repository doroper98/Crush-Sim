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

$Zip = Join-Path $Dest $Asset

if (-not (Test-Path $Zip)) {
    Write-Host "Downloading $Url ..."
    try {
        # The progress bar makes Windows PowerShell 5.1 downloads drastically slower.
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $Url -OutFile $Zip
    } catch {
        Write-Warning "Download failed ($($_.Exception.Message)) — the asset name may differ for tag $Tag."
        Write-Warning "Download manually from https://github.com/OpenRadioss/OpenRadioss/releases/tag/$Tag"
        Write-Warning "and put the zip into $Dest\, then re-run this script to extract it."
        exit 1
    }
} else {
    Write-Host "Using already-downloaded $Zip"
}

# Windows PowerShell 5.1's Expand-Archive chokes on this archive; the built-in
# bsdtar (Windows 10 1803+) handles it fine, so prefer it.
try {
    if (Get-Command tar -ErrorAction SilentlyContinue) {
        tar -xf $Zip -C $Dest
        if ($LASTEXITCODE -ne 0) { throw "tar exited with code $LASTEXITCODE" }
    } else {
        Expand-Archive -Path $Zip -DestinationPath $Dest -Force
    }
    Remove-Item $Zip
    Write-Host "Extracted to $Dest\. Now:"
    Write-Host "  1. Follow the README inside the extracted archive for env setup (paths, OMP threads)."
    Write-Host "  2. Set install_root (or the starter/engine paths) in configs\solver.yaml."
    Write-Host "  3. Re-run: csim doctor"
} catch {
    Write-Warning "Extraction failed: $($_.Exception.Message)"
    Write-Warning "The zip is kept at $Zip — extract it manually (right-click > Extract All,"
    Write-Warning "or 'tar -xf $Asset' inside $Dest\), then update configs\solver.yaml."
    exit 1
}
