#!/usr/bin/env bash
# Best-effort helper: download an official OpenRadioss release into tools/openradioss/ (git-ignored).
# The version tag is pinned in configs/solver.yaml. Per SPEC §9, installation and run
# flags must follow the official OpenRadioss README — verify after download:
#   https://github.com/OpenRadioss/OpenRadioss/releases
# Usage: scripts/install_openradioss.sh [release-tag]
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-$(python3 - <<'EOF'
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path("configs/solver.yaml").read_text())
print(cfg.get("version_tag") or cfg.get("tag") or "")
EOF
)}"
if [ -z "$TAG" ]; then
  echo "ERROR: no release tag given and none pinned in configs/solver.yaml." >&2
  echo "Pick one from https://github.com/OpenRadioss/OpenRadioss/releases and pin it." >&2
  exit 1
fi

ASSET="OpenRadioss_linux64.zip"
URL="https://github.com/OpenRadioss/OpenRadioss/releases/download/${TAG}/${ASSET}"
DEST="tools/openradioss"
mkdir -p "$DEST"

echo "Downloading ${URL} ..."
if curl -fL --retry 3 -o "$DEST/$ASSET" "$URL"; then
  (cd "$DEST" && unzip -oq "$ASSET" && rm -f "$ASSET")
  echo "Extracted to $DEST/. Now:"
  echo "  1. Follow the README inside the extracted archive for env setup (paths, OMP threads)."
  echo "  2. Point configs/solver.yaml starter/engine paths at the extracted executables."
  echo "  3. Re-run: csim doctor"
else
  echo "Download failed — the asset name may differ for tag ${TAG}." >&2
  echo "Download manually from https://github.com/OpenRadioss/OpenRadioss/releases/tag/${TAG}" >&2
  echo "and extract into ${DEST}/, then update configs/solver.yaml." >&2
  exit 1
fi
