#!/usr/bin/env bash
# Crush-Sim bootstrap — Linux / WSL2 / macOS
# Usage: scripts/bootstrap.sh   (run from the repo root)
# Creates .venv, installs the package + dev deps, then runs `csim doctor`.
set -euo pipefail

cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), f"Python 3.11+ required, got {sys.version}"'

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -e ".[dev]" || pip install -e .

echo
echo "=== Environment self-check (Phase 0 Day-1 gate) ==="
csim doctor || true

cat <<'EOF'

Bootstrap complete.
  Activate the environment:   source .venv/bin/activate
  Run the smoke pipeline:     csim all --config configs/cases/lc2_default.yaml
  Install the solver:         scripts/install_openradioss.sh   (see configs/solver.yaml)
  Fetch legacy reference:     scripts/fetch_legacy.sh          (only needed to re-run FR-10 harvest)
EOF
