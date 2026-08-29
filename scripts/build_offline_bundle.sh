#!/usr/bin/env bash
# Build an air-gapped install bundle on a CONNECTED machine (spec: 폐쇄망 배포).
#
# Produces offline_bundle/ next to the repo root:
#   wheelhouse/   every wheel needed for `pip install crushsim[ui,cad,dev]`
#   (the repo itself, tools/openradioss included, is copied by you - git
#    archive or a plain folder copy; nothing in it needs the network)
#
# On the AIR-GAPPED machine run scripts/install_offline.sh from the repo root
# with offline_bundle/ alongside it.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="offline_bundle/wheelhouse"
mkdir -p "$OUT"
python -m pip download --dest "$OUT" \
  -e ".[ui,cad]" \
  pip setuptools wheel uvicorn fastapi
echo "wheelhouse ready: $OUT ($(du -sh "$OUT" | cut -f1))"
echo "Copy the repository folder AND offline_bundle/ to the air-gapped machine."
