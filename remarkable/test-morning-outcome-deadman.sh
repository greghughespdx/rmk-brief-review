#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/morning-outcome-deadman.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DATE=2026-08-20
FAIL=0

cat > "$WORK/bus-alert" <<'EOF'
#!/usr/bin/env bash
printf '%s\t%s\t%s\n' "$BUS_ALERT_RECIPIENTS" "$1" "$2" >> "$BUS_LOG"
[ "${BUS_FAIL:-0}" != 1 ]
EOF
chmod +x "$WORK/bus-alert"

run() {
  MB_DATE="$DATE" MB_OUTCOME_RECORD_DIR="$WORK/records" \
  MB_DEADMAN_MARKER_DIR="$WORK/markers" MB_BUS_ALERT="$WORK/bus-alert" \
  MB_DEADMAN_ALLOW_OUTBOUND="${ALLOW:-1}" BUS_LOG="$WORK/bus.log" \
  BUS_FAIL="${BUS_FAIL:-0}" "$SCRIPT" >"$WORK/out" 2>"$WORK/err"
  RC=$?
}
ok() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

mkdir -p "$WORK/records"
printf '{"date":"%s","outcome":"Morning delivery: ok","send_status":"sent"}\n' "$DATE" > "$WORK/records/$DATE.json"
: > "$WORK/bus.log"
run
ok test "$RC" -eq 0
ok test ! -s "$WORK/bus.log"

rm -f "$WORK/records/$DATE.json"
run
ok test "$RC" -eq 1
ok grep -q 'operators' "$WORK/bus.log"
ok test -s "$WORK/markers/$DATE.pending"

printf '{broken\n' > "$WORK/records/$DATE.json"
: > "$WORK/bus.log"
run
ok test "$RC" -eq 1
ok grep -q 'does not prove the line was sent' "$WORK/bus.log"

printf '{"date":"%s","outcome":"Morning delivery: uncertain","send_status":"prepared"}\n' "$DATE" > "$WORK/records/$DATE.json"
: > "$WORK/bus.log"
run
ok test "$RC" -eq 1
ok grep -q 'does not prove the line was sent' "$WORK/bus.log"

printf '{"date":"%s","outcome":"Morning delivery: failed","send_status":"failed"}\n' "$DATE" > "$WORK/records/$DATE.json"
: > "$WORK/bus.log"
run
ok test "$RC" -eq 1
ok grep -q 'does not prove the line was sent' "$WORK/bus.log"

: > "$WORK/bus.log"
BUS_FAIL=1 run
ok test "$RC" -eq 1
ok test -s "$WORK/markers/$DATE.pending"
BUS_FAIL=0

: > "$WORK/bus.log"
ALLOW=0 run
ok test "$RC" -eq 1
ok test ! -s "$WORK/bus.log"

exit "$FAIL"
