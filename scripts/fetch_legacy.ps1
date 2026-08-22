# Fetch the legacy repository (read-only reference, git-ignored) into legacy_ref\.
# Only needed to re-run the FR-10 material harvester; the harvested YAML cards
# are already committed under configs\materials\harvested\.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Test-Path "legacy_ref\.git") {
    Write-Host "legacy_ref\ already present."
} else {
    git clone --depth 1 https://github.com/doroper98/can_crush_sim.git legacy_ref
}
Write-Host "Harvest input: legacy_ref\src\engine\MaterialModel.ts"
Write-Host "Re-run harvest: csim harvest --input legacy_ref\src\engine\MaterialModel.ts --outdir configs\materials\harvested"
