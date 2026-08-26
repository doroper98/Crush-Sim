#!/usr/bin/env bash
# Install Crush-Sim on an AIR-GAPPED machine from offline_bundle/wheelhouse.
# Run from the repository root. Requires only a system Python >= 3.11.
set -euo pipefail
cd "$(dirname "$0")/.."
WHEELHOUSE="${1:-offline_bundle/wheelhouse}"
if [ ! -d "$WHEELHOUSE" ]; then
  echo "wheelhouse not found: $WHEELHOUSE (build it with scripts/build_offline_bundle.sh)" >&2
  exit 1
fi
python -m venv .venv
. .venv/bin/activate
pip install --no-index --find-links "$WHEELHOUSE" -e ".[ui,cad]"
csim doctor || true
echo
echo "Done. Start the UI with:  . .venv/bin/activate && csim ui"
echo "(The UI and viewers use bundled fonts and make no network requests.)"
