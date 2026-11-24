#!/bin/bash
# Regression test for morning-status-watch.sh (the reader, 2026-10-05: the 06:20
# "on your tablet now" text arrived every morning after the 06:00 text was
# reworded on 2026-09-16 to "Morning briefing: delivered").
# Every run uses a scratch outcome directory, a stub tablet listing and a stub
# text sender; nothing live is read or written.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH="$DIR/morning-status-watch.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP:?}"' EXIT
D="$(date +%F)"
PASS=0; FAIL=0

run_case() { # name outcome-text expect-sent(yes/no)
  local name="$1" outcome="$2" expect="$3" dir="$TMP/$1"
  mkdir -p "$dir"
  python3 -c 'import json,sys;json.dump({"date":sys.argv[1],"outcome":sys.argv[2],"send_status":"sent"},open(sys.argv[3],"w"))' "$D" "$outcome" "$dir/$D.json"
  printf '#!/bin/bash\necho "[f] morning-briefing-%s"; echo "[f] decisions-%s"\n' "$D" "$D" > "$dir/rmapi"
  printf '#!/bin/bash\necho "$@" >> "%s/sent.txt"\n' "$dir" > "$dir/alert"
  chmod +x "$dir/rmapi" "$dir/alert"
  MB_OUTCOME_RECORD_DIR="$dir" MB_WATCH_LOG="$dir/log" MB_RMAPI="$dir/rmapi" \
    MB_IMESSAGE_ALERT="$dir/alert" MB_WATCH_END_HOUR=24 bash "$WATCH"
  local got=no; [ -s "$dir/sent.txt" ] && got=yes
  if [ "$got" = "$expect" ]; then PASS=$((PASS+1)); echo "PASS: $name"; else FAIL=$((FAIL+1)); echo "FAIL: $name (sent=$got, expected $expect)"; fi
}

# The real 06:00 text from 2026-10-05.
run_case current-wording-clean $'Morning Status Check:\n- Morning briefing: delivered\n- Decisions document: delivered' no
run_case old-wording-clean 'Morning delivery: briefing delivered; decisions delivered; calendar ok.' no
run_case both-missing-then-arrive $'Morning Status Check:\n- Morning briefing: not delivered\n- Decisions document: not delivered' yes
run_case decisions-missing-then-arrive $'Morning Status Check:\n- Morning briefing: delivered\n- Decisions document: not delivered, no questions for you' yes

echo "morning-status-watch results: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
