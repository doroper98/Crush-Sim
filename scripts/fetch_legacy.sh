#!/usr/bin/env bash
# Fetch the legacy repository (read-only reference, git-ignored) into legacy_ref/.
# Only needed to re-run the FR-10 material harvester; the harvested YAML cards
# are already committed under configs/materials/harvested/.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -d legacy_ref/.git ]; then
  echo "legacy_ref/ already present."
else
  git clone --depth 1 https://github.com/doroper98/can_crush_sim.git legacy_ref
fi
echo "Harvest input: legacy_ref/src/engine/MaterialModel.ts"
echo "Re-run harvest: csim harvest --input legacy_ref/src/engine/MaterialModel.ts --outdir configs/materials/harvested"
