#!/usr/bin/env bash
# 07:00 dead-man for the 06:00 morning outcome line.
#
# This checker never messages the reader. It alerts the orchestrator and the cross-reviewer over the bus
# when today's outcome record is missing or unreadable. It is deliberately a
# separate scheduled process so a morning-dashboard launch failure is visible.

set -uo pipefail

TODAY="${MB_DATE:-$(date +%F)}"
RECORD_DIR="${MB_OUTCOME_RECORD_DIR:-$HOME/.morning-outcomes}"
RECORD="$RECORD_DIR/$TODAY.json"
BUS_ALERT="${MB_BUS_ALERT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hooks/alert.sh}"
MARKER_DIR="${MB_DEADMAN_MARKER_DIR:-$HOME/.morning-outcome-deadman}"
ALLOW="${MB_DEADMAN_ALLOW_OUTBOUND:-0}"

mkdir -p "$MARKER_DIR" 2>/dev/null || true
chmod 700 "$MARKER_DIR" 2>/dev/null || true

alert_agents() {
  local subject="$1" body="$2" slug="$3"
  if [ "$ALLOW" != "1" ]; then
    echo "DEAD-MAN ALERT SUPPRESSED: $subject" >&2
    return 90
  fi
  BUS_ALERT_RECIPIENTS="${MB_BUS_ALERT_RECIPIENTS:-operators}" \
    "$BUS_ALERT" "$subject" "$body" "$slug"
}

reason=""
if [ ! -e "$RECORD" ]; then
  reason="no outcome-line record exists for $TODAY"
elif ! python3 - "$RECORD" "$TODAY" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected_date = sys.argv[2]
try:
    outcome_record = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if not isinstance(outcome_record, dict):
    raise SystemExit(1)
if outcome_record.get("date") != expected_date:
    raise SystemExit(1)
if not isinstance(outcome_record.get("outcome"), str) or not outcome_record["outcome"].strip():
    raise SystemExit(1)
if outcome_record.get("send_status") != "sent":
    raise SystemExit(1)
PY
then
  reason="the outcome-line record for $TODAY is unreadable, invalid, or does not prove the line was sent"
fi

if [ -z "$reason" ]; then
  rm -f "$MARKER_DIR/$TODAY.pending" 2>/dev/null || true
  echo "morning outcome dead-man: record present and valid for $TODAY"
  exit 0
fi

marker="$MARKER_DIR/$TODAY.pending"
marker_tmp="$(mktemp "$MARKER_DIR/.pending.XXXXXX" 2>/dev/null || true)"
if [ -n "$marker_tmp" ]; then
  chmod 600 "$marker_tmp" 2>/dev/null || true
  printf '%s\n' "$reason" > "$marker_tmp" && mv "$marker_tmp" "$marker"
fi
if alert_agents "Morning outcome is missing ($TODAY)" \
  "The independent 07:00 check found that $reason. The reader was not messaged by this check. Inspect the 06:00 LaunchAgent and /tmp/morning-dashboard.log." \
  "morning-outcome-deadman"; then
  echo "morning outcome dead-man: alert sent: $reason" >&2
else
  echo "morning outcome dead-man: ALERT FAILED: $reason" >&2
fi
exit 1
