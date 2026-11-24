#!/usr/bin/env bash
# Render the worked example with the repository's own code.
#
#   examples/build-example.sh
#
# 1. verify-queue.py checks every Active row against the card contract and
#    writes a filtered queue plus a verification report;
# 2. today_page.py generate renders the Decisions PDF and its manifest.
#
# Nothing touches the tablet. The archive and answer state live in a scratch
# folder, so the render starts from "nothing answered yet". The verification
# timestamp is pinned so the example renders the same way every time.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
DATE="2026-01-15"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/archive/decisions" "$HERE/output"

export RMW_CONFIG=none
export RMW_PATHS_HOME="$SCRATCH"
export RMW_PATHS_QUEUE="$HERE/decisions-queue.md"
export RMW_PATHS_CONTENT_ROOT="$HERE"

"$PY" "$ROOT/remarkable/verify-queue.py" \
  --queue "$HERE/decisions-queue.md" \
  --out-queue "$SCRATCH/verified-queue.md" \
  --report "$SCRATCH/verify-report.json" \
  --render-date "$DATE"

"$PY" - "$SCRATCH/verify-report.json" <<'PIN'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
report = json.loads(path.read_text())
report["verified_at"] = "2026-01-15T05:00:00-08:00"
path.write_text(json.dumps(report, indent=2))
PIN

"$PY" "$ROOT/remarkable/today_page.py" generate \
  --queue "$SCRATCH/verified-queue.md" \
  --verify-report "$SCRATCH/verify-report.json" \
  --out-dir "$HERE/output" \
  --state "$SCRATCH/archive/question-state.json" \
  --date "$DATE"

cp "$SCRATCH/verify-report.json" "$HERE/output/verify-report.json"
echo "rendered: $HERE/output/decisions-$DATE.pdf"
