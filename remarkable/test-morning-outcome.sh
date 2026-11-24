#!/usr/bin/env bash
# Binding tests for the 06:00 morning delivery job (mc-p9xd).
#
# WHY THIS FILE EXISTS. The entire defect class here is FAILURES THAT PRODUCE
# SILENCE: on 2026-08-17, 08-18 and 08-19 the job hit a missing overnight
# handoff, exited at that branch, never attempted the Decisions document, and
# told the reader nothing on a channel he reads. So every failure path gets exercised,
# not just the happy one.
#
# The rule this file follows: drive the REAL morning-dashboard.sh. Nothing here
# reimplements its logic. Only its EXTERNAL dependencies are substituted - rmapi,
# gws, the two push scripts, the two generators, the bus helper and the iMessage
# helper - through the override variables the production script already reads.
# No device is touched and no message is sent.
#
# Usage: remarkable/test-morning-outcome.sh
# Exit 0 = every case holds. Exit 1 = a regression.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/morning-dashboard.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DATE="2026-08-20"
FAILURES=0
CASE=""
note_fail() { echo "  [FAIL] $1"; FAILURES=$((FAILURES + 1)); }
note_pass() { echo "  [ok]   $1"; }

# ---------------------------------------------------------------------------
# Fakes. Each reads its own FAKE_* switch out of the environment at call time,
# so one set of stubs covers every case.
# ---------------------------------------------------------------------------
BIN="$WORK/bin"
mkdir -p "$BIN"

cat > "$BIN/gws" <<'EOF'
#!/usr/bin/env bash
if [ "${FAKE_CAL_FAIL:-0}" = "1" ]; then
  echo "gws: Google API error: invalid_rapt (401)" >&2
  exit 1
fi
cat <<'JSON'
{"items":[{"summary":"Standup","start":{"dateTime":"2026-08-20T09:00:00-07:00"}}]}
JSON
EOF

# Fake rmapi with a tiny on-disk "device" so put/ls/rm are actually consistent.
cat > "$BIN/rmapi" <<'EOF'
#!/usr/bin/env bash
DEVICE="${FAKE_DEVICE_DIR:?FAKE_DEVICE_DIR unset}"
cmd="${1:-}"; shift || true
# ls and geta are reads; mkdir/put/rm change the device and are recorded.
case "$cmd" in mkdir|put|rm) printf 'rmapi %s %s\n' "$cmd" "${1:-}" >> "${OUTBOUND_RECORDER:?}" ;; esac
case "$cmd" in
  mkdir) mkdir -p "$DEVICE/${1#/}"; exit 0 ;;
  put)
    [ "${FAKE_RMAPI_PUT_FAIL:-0}" = "1" ] && { echo "rmapi: HTTP 400 (unsorted root index)" >&2; exit 1; }
    src="$1"; dest="${2:-/}"
    mkdir -p "$DEVICE/${dest#/}"
    base="$(basename "$src")"; base="${base%.*}"
    : > "$DEVICE/${dest#/}/$base"
    exit 0 ;;
  ls)
    dir="$DEVICE/${1#/}"
    [ -d "$dir" ] || exit 0
    for f in "$dir"/*; do [ -e "$f" ] || continue; printf '[f]\t%s\n' "$(basename "$f")"; done
    exit 0 ;;
  rm)
    [ "${FAKE_RMAPI_RM_FAIL:-0}" = "1" ] && { echo "rmapi: delete refused" >&2; exit 1; }
    rm -f "$DEVICE/${1#/}"; exit 0 ;;
  geta|get) exit 1 ;;
  *) exit 0 ;;
esac
EOF

cat > "$BIN/push-brief.sh" <<'EOF'
#!/usr/bin/env bash
printf 'push-brief %s\\n' "$1" >> "${OUTBOUND_RECORDER:?}"
[ "${FAKE_BRIEF_PUSH_FAIL:-0}" = "1" ] && { echo "push-brief: upload rejected" >&2; exit 1; }
echo "push-brief: pushed $1"
exit 0
EOF

cat > "$BIN/push-decisions.sh" <<'EOF'
#!/usr/bin/env bash
printf 'push-decisions\\n' >> "${OUTBOUND_RECORDER:?}"
[ "${FAKE_DECISIONS_RENDER_FAIL:-0}" = "1" ] && { echo "push-decisions: generation refused" >&2; exit 2; }
[ "${FAKE_DECISIONS_EMPTY_DOC:-0}" = "1" ] && { echo "push-decisions: empty-doc tripwire" >&2; exit 3; }
[ "${FAKE_DECISIONS_REVIEW_GATE:-0}" = "1" ] && { echo "push-decisions: no review receipt" >&2; exit 4; }
[ "${FAKE_DECISIONS_NOTHING:-0}" = "1" ] && exit 5
[ "${FAKE_DECISIONS_CRASH:-0}" = "1" ] && { echo "push-decisions: generator crashed" >&2; exit 6; }
[ "${FAKE_DECISIONS_PUSH_FAIL:-0}" = "1" ] && { echo "push-decisions: upload rejected" >&2; exit 1; }
echo "push-decisions: pushed"
exit 0
EOF

cat > "$BIN/verify-queue.py" <<'EOF'
import os, sys
if os.environ.get("FAKE_VERIFY_FAIL") == "1":
    sys.stderr.write("verify-queue: freshness check blew up\n")
    sys.exit(1)
out = sys.argv[sys.argv.index("--out-queue") + 1]
rep = sys.argv[sys.argv.index("--report") + 1]
open(out, "w").write("- a verified decision\n")
open(rep, "w").write("{}\n")
EOF

cat > "$BIN/generate-dashboard.py" <<'EOF'
import os, sys
if os.environ.get("FAKE_GENERATE_FAIL") == "1":
    sys.stderr.write("generate-dashboard: render failed\n")
    sys.exit(1)
out = sys.argv[sys.argv.index("-o") + 1]
open(out, "w").write("# Morning Briefing\n\nbody\n")
EOF

cat > "$BIN/rotate-briefs.sh" <<'EOF'
#!/usr/bin/env bash
printf 'rotate-briefs\\n' >> "${OUTBOUND_RECORDER:?}"
echo "rotate: nothing to do"
EOF

cat > "$BIN/bus-alert.sh" <<'EOF'
#!/usr/bin/env bash
printf 'bus-alert %s\n' "$1" >> "${OUTBOUND_RECORDER:?}"
printf '%s|%s\n' "${BUS_ALERT_RECIPIENTS:-}" "$1" >> "${FAKE_BUS_LOG:?FAKE_BUS_LOG unset}"
EOF

cat > "$BIN/imessage-alert.sh" <<'EOF'
#!/usr/bin/env bash
printf 'imessage %s\n' "$1" >> "${OUTBOUND_RECORDER:?}"
python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1" >> "${FAKE_IMESSAGE_LOG:?FAKE_IMESSAGE_LOG unset}"
[ "${FAKE_IMESSAGE_FAIL:-0}" = "1" ] && { echo "imessage-alert: bridge refused" >&2; exit 1; }
exit 0
EOF

chmod +x "$BIN"/*

# ---------------------------------------------------------------------------
# Harness. run_case <name> [env assignments...] -> sets LOG, LINE, CODE.
# ---------------------------------------------------------------------------
LOG=""; LINE=""; CODE=0; RUN_DIR=""
HANDOFF="none"          # none | full | empty-section
REVIEWED=0
ALLOW_OUTBOUND=1        # cases that test the guard set this to 0
PENDING_FILE=""         # empty = per-case file
AGENT_MARKER_DIR=""     # empty = per-case directory

run_case() {
  CASE="$1"; shift
  echo
  echo "CASE: $CASE"
  RUN_DIR="$WORK/case-$(printf '%s' "$CASE" | tr -c 'a-zA-Z0-9' '-')"
  rm -rf "$RUN_DIR"
  mkdir -p "$RUN_DIR/device" "$RUN_DIR/repo/thoughts/handoffs" "$RUN_DIR/archive" "$RUN_DIR/home" "$RUN_DIR/tmp"
  : > "$RUN_DIR/outbound.log"
  if [ "$REVIEWED" -eq 1 ]; then
    mkdir -p "$RUN_DIR/archive/$DATE"
    printf '{}\n' > "$RUN_DIR/archive/$DATE/review-receipt.json"
    printf '{}\n' > "$RUN_DIR/archive/$DATE/decisions-$DATE.manifest.json"
    printf 'fixture\n' > "$RUN_DIR/archive/$DATE/decisions-$DATE.pdf"
  fi

  local hf="$RUN_DIR/repo/thoughts/handoffs/$DATE-overnight.md"
  case "$HANDOFF" in
    full)          printf '## Morning Briefing\n\nEverything ran clean overnight.\n\n## Completed\n\n- a thing\n' > "$hf" ;;
    empty-section) printf '## Completed\n\n- a thing, and no briefing section anywhere in this file\n' > "$hf" ;;
    none)          : ;;
  esac

  env \
    PATH="$BIN:$PATH" \
    HOME="$RUN_DIR/home" \
    MB_DATE="$DATE" \
    MB_RETRIES=0 \
    MB_RETRY_DELAY=0 \
    MB_TMP_DIR="$RUN_DIR/tmp" \
    MB_ALLOW_OUTBOUND="$ALLOW_OUTBOUND" \
    OUTBOUND_RECORDER="$RUN_DIR/outbound.log" \
    MB_REPO="$RUN_DIR/repo" \
    RMW_BRIEFING_HANDOFF_PATTERN='thoughts/handoffs/{date}-overnight.md' \
    MB_GENERATE="$BIN/generate-dashboard.py" \
    MB_PUSH_BRIEF="$BIN/push-brief.sh" \
    MB_PUSH_DECISIONS="$BIN/push-decisions.sh" \
    MB_VERIFY_QUEUE="$BIN/verify-queue.py" \
    MB_ROTATE="$BIN/rotate-briefs.sh" \
    MB_BUS_ALERT="$BIN/bus-alert.sh" \
    MB_IMESSAGE_ALERT="$BIN/imessage-alert.sh" \
    MB_PENDING_FILE="${PENDING_FILE:-$RUN_DIR/pending}" \
    MB_OUTCOME_RECORD_DIR="$RUN_DIR/outcomes" \
    MB_AGENT_ALERT_MARKER_DIR="${AGENT_MARKER_DIR:-$RUN_DIR/agent-alerts}" \
    RMAPI="$BIN/rmapi" \
    FAKE_DEVICE_DIR="$RUN_DIR/device" \
    FAKE_BUS_LOG="$RUN_DIR/bus.log" \
    FAKE_IMESSAGE_LOG="$RUN_DIR/imessage.log" \
    REMARKABLE_TODAY_ARCHIVE="$RUN_DIR/archive" \
    REMARKABLE_TODAY_REGISTRY="$RUN_DIR/registry.json" \
    "$@" \
    "$SCRIPT" > "$LOG_TARGET" 2>&1
  CODE=$?
  LOG="$LOG_TARGET"
  LINE="$(awk '/^OUTCOME LINE: / {sub(/^OUTCOME LINE: /, ""); print; seen=1; next} seen && /^- / {print; next} seen {exit}' "$LOG")"
  echo "  exit=$CODE"
  echo "  line=$LINE"
}

# LOG_TARGET has to be settled before the run_dir exists, so it lives outside.
LOG_TARGET="$WORK/last-run.log"

expect_code() {
  if [ "$CODE" = "$1" ]; then note_pass "exit code $1"; else note_fail "$CASE: expected exit $1, got $CODE"; fi
}
expect_line() {
  if printf '%s' "$LINE" | grep -qF "$1"; then note_pass "line says: $1"
  else note_fail "$CASE: outcome line missing \"$1\" (got: $LINE)"; fi
}
expect_not_line() {
  if printf '%s' "$LINE" | grep -qF "$1"; then note_fail "$CASE: outcome line should NOT say \"$1\" (got: $LINE)"
  else note_pass "line does not say: $1"; fi
}
expect_sent() {
  local n
  n=$(count_lines "$RUN_DIR/imessage.log")
  if [ "$n" = "1" ]; then note_pass "exactly one message sent to the reader"
  else note_fail "$CASE: expected exactly 1 message to the reader, got $n"; fi
}
count_lines() {  # non-blank lines in a file that may not exist
  [ -f "$1" ] || { echo 0; return 0; }
  local n; n=$(grep -c '[^[:space:]]' "$1" 2>/dev/null)
  echo "${n:-0}"
}

expect_no_outbound() {
  local n
  n=$(count_lines "$RUN_DIR/outbound.log")
  if [ "$n" = "0" ]; then note_pass "ZERO outbound calls (nothing sent, nothing written to the device)"
  else note_fail "$CASE: $n outbound call(s) escaped the guard: $(tr '\n' '; ' < "$RUN_DIR/outbound.log")"; fi
}
expect_outbound() {
  local n
  n=$(count_lines "$RUN_DIR/outbound.log")
  if [ "$n" -gt 0 ]; then note_pass "$n outbound call(s) made, as the flag allows"
  else note_fail "$CASE: the flag was set but nothing went out"; fi
}
expect_decisions_pushed() {
  if grep -q 'push-decisions: pushed' "$LOG"; then note_pass "decisions document actually produced"
  else note_fail "$CASE: the decisions document was never produced"; fi
}

# ===========================================================================
# CASE 1 - no overnight handoff. The exact three-day outage.
# The briefing is withheld AND the decisions document still gets produced.
# ===========================================================================
HANDOFF=none
run_case "no handoff present"
expect_code 2
expect_line "Morning briefing: not generated (no overnight handoff to process)"
expect_line "Decisions document: delivered"
expect_decisions_pushed          # the decoupling proof
expect_sent

# ===========================================================================
# CASE 2 - handoff exists but has no Morning Briefing section.
# ===========================================================================
HANDOFF=empty-section
run_case "handoff present but empty briefing section"
expect_code 2
expect_line "Morning briefing: not generated (no briefing in the overnight handoff)"
expect_line "Decisions document: delivered"
expect_decisions_pushed
expect_sent

# ===========================================================================
# CASE 3 - decisions generation fails (freshness pass blows up).
# The briefing must still be delivered.
# ===========================================================================
HANDOFF=full
run_case "decisions generation failing" FAKE_VERIFY_FAIL=1
expect_code 2
expect_line "Morning briefing: delivered"
expect_line "Decisions document: not generated (the items could not be checked as current)"
expect_sent

# ===========================================================================
# CASE 3b - the queue verifies, but the Decisions generator refuses an input.
# The outcome must name generation, not imply a delivery verification failure.
# ===========================================================================
HANDOFF=full
run_case "decisions render refusing" FAKE_DECISIONS_RENDER_FAIL=1
expect_code 2
expect_line "Morning briefing: delivered"
expect_line "Decisions document: not generated (the document could not be generated)"
expect_sent

# ===========================================================================
# CASE 3c - the empty-doc tripwire: active rows exist but 0 cards survived
# verification, so push-decisions.sh refuses to ship a blank sheet.
# ===========================================================================
HANDOFF=full
run_case "decisions empty-doc tripwire" FAKE_DECISIONS_EMPTY_DOC=1
expect_code 2
expect_line "Morning briefing: delivered"
expect_line "your decisions are not ready yet; agents are working on them"
expect_sent

HANDOFF=full
run_case "no active decisions is a successful morning" FAKE_DECISIONS_NOTHING=1
expect_code 0
expect_line "no decisions, so no document"
expect_sent

REVIEWED=1
run_case "existing zero card document is not delivered" FAKE_DECISIONS_NOTHING=1
expect_code 0
expect_line "no decisions, so no document"
expect_sent
run_case "existing all held document stays an agent failure" FAKE_DECISIONS_EMPTY_DOC=1
expect_code 2
expect_line "your decisions are not ready yet; agents are working on them"
expect_not_line "no decisions"
expect_sent
REVIEWED=0

# ===========================================================================
# CASE 3d - the review gate: the rendered PDF and manifest have no valid
# review receipt (mc-iacbw).
# ===========================================================================
HANDOFF=full
run_case "decisions review gate" FAKE_DECISIONS_REVIEW_GATE=1
expect_code 2
expect_line "Morning briefing: delivered"
expect_line "Decisions document: not generated (the rendered document has no valid review receipt)"
expect_sent

# ===========================================================================
# CASE 3e - the generator crashes outright (today_page.py EXIT_CRASH).
# ===========================================================================
HANDOFF=full
run_case "decisions generator crash" FAKE_DECISIONS_CRASH=1
expect_code 2
expect_line "Morning briefing: delivered"
expect_line "Decisions document: not generated (the generator crashed)"
expect_sent

# ===========================================================================
# CASE 4 - decisions push fails.
# ===========================================================================
HANDOFF=full
run_case "decisions push failing" FAKE_DECISIONS_PUSH_FAIL=1
expect_code 2
expect_line "Morning briefing: delivered"
expect_line "Decisions document: not generated (delivery could not be verified)"
expect_sent

# ===========================================================================
# CASE 4b - the briefing document itself fails to render. The decisions
# document must be unaffected, because it already ran.
# ===========================================================================
HANDOFF=full
run_case "briefing generation failing" FAKE_GENERATE_FAIL=1
expect_code 2
expect_line "Morning briefing: not generated (the document could not be built)"
expect_line "Decisions document: delivered"
expect_decisions_pushed
expect_sent

# ===========================================================================
# CASE 5 - calendar unavailable (the live invalid_rapt condition).
# Both documents still land; the run is degraded, not failed.
# ===========================================================================
HANDOFF=full
run_case "calendar unavailable" FAKE_CAL_FAIL=1
expect_code 1
expect_line "Morning briefing: delivered"
expect_line "Decisions document: delivered"
expect_line "Calendar: unavailable (Google sign-in needs renewing)"
expect_sent
if grep -q '^operators|' "$RUN_DIR/bus.log"; then note_pass "component alerts target both agents"
else note_fail "$CASE: component alerts did not target both agents"; fi

# ===========================================================================
# CASE 6 - reMarkable writes rejected. The 2026-08-19 HTTP 400 condition.
# ===========================================================================
HANDOFF=full
run_case "remarkable write failing" FAKE_RMAPI_PUT_FAIL=1 FAKE_BRIEF_PUSH_FAIL=1 FAKE_DECISIONS_PUSH_FAIL=1
expect_code 2
expect_line "Tablet write check: failed"
expect_line "Morning briefing: not generated (delivery could not be verified)"
expect_line "Decisions document: not generated (delivery could not be verified)"
expect_sent

# ===========================================================================
# CASE 6b - the probe writes but cannot clean up after itself.
# ===========================================================================
HANDOFF=full
run_case "probe cleanup failing" FAKE_RMAPI_RM_FAIL=1
expect_code 1
expect_line "Tablet write check: passed but cleanup failed"
expect_line "Morning briefing: delivered"

# ===========================================================================
# CASE 7 - the iMessage send itself fails. It must be recorded durably...
# ===========================================================================
HANDOFF=full
SHARED_PENDING="$WORK/shared-pending"
rm -f "$SHARED_PENDING"
PENDING_FILE="$SHARED_PENDING"
run_case "imessage send failing" FAKE_IMESSAGE_FAIL=1
expect_code 1
expect_line "Morning briefing: delivered"
if [ -s "$SHARED_PENDING" ]; then note_pass "the failed send was recorded durably"
else note_fail "$CASE: a failed iMessage send left no durable record"; fi
if grep -q 'Morning outcome message could NOT be sent' "$RUN_DIR/bus.log" 2>/dev/null; then
  note_pass "the failed send was raised on the bus"
else note_fail "$CASE: a failed iMessage send was not raised on the bus"; fi
if python3 - "$RUN_DIR/outcomes/$DATE.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1]))["send_status"] == "failed"
PY
then note_pass "the failed send has an explicit failed record"
else note_fail "$CASE: the failed send record is missing or dishonest"; fi

# ==========================================================================
# CASE 7a - sender missing. The write-ahead marker must exist before the helper
# is inspected, so this failure is recoverable too.
# ==========================================================================
HANDOFF=full
PENDING_FILE="$WORK/missing-sender-pending"
run_case "imessage helper missing" MB_IMESSAGE_ALERT="$WORK/not-installed"
expect_code 1
if [ -s "$PENDING_FILE" ]; then note_pass "a missing sender retained the write-ahead marker"
else note_fail "$CASE: missing sender left no write-ahead marker"; fi
if python3 - "$RUN_DIR/outcomes/$DATE.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1]))["send_status"] == "failed"
PY
then note_pass "a missing sender produced a failed record"
else note_fail "$CASE: missing sender record is missing or dishonest"; fi
PENDING_FILE="$SHARED_PENDING"

# ===========================================================================
# CASE 7b - ...and surfaced on the NEXT run rather than vanishing.
# ===========================================================================
HANDOFF=full
PENDING_FILE="$SHARED_PENDING"
run_case "next run after failed send"
expect_code 0
expect_not_line "never reached you"
if grep -q 'Earlier morning outcome needs review' "$RUN_DIR/bus.log" 2>/dev/null; then
  note_pass "the earlier uncertain send was raised to the agents"
else note_fail "$CASE: the earlier uncertain send was not raised to the agents"; fi
if [ -f "$SHARED_PENDING" ]; then note_fail "$CASE: the pending record was not cleared after a good send"
else note_pass "the pending record cleared once the reader was reached"; fi

PENDING_FILE=""

# ==========================================================================
# CASE 7c - local marker path attacks. Recovery must not read a symlink and the
# write-ahead pending write must replace, not follow, a symlink.
# ==========================================================================
HANDOFF=full
AGENT_MARKER_DIR="$WORK/shared-agent-markers"
mkdir -p "$AGENT_MARKER_DIR"
printf 'DO-NOT-SEND-THIS\n' > "$WORK/marker-victim"
ln -s "$WORK/marker-victim" "$AGENT_MARKER_DIR/forged.pending"
printf 'DO-NOT-SEND-REGULAR\n' > "$AGENT_MARKER_DIR/unrecognized.pending"
PENDING_FILE="$WORK/symlink-pending"
printf 'KEEP-PENDING-VICTIM\n' > "$WORK/pending-victim"
ln -s "$WORK/pending-victim" "$PENDING_FILE"
run_case "marker symlinks refused" FAKE_IMESSAGE_FAIL=1
if grep -q 'DO-NOT-SEND-THIS' "$RUN_DIR/bus.log" 2>/dev/null; then
  note_fail "$CASE: a symlinked agent marker leaked its target"
else note_pass "symlinked agent marker was not read or sent"; fi
if grep -q 'DO-NOT-SEND-REGULAR' "$RUN_DIR/bus.log" 2>/dev/null; then
  note_fail "$CASE: an unrecognized regular marker was sent"
else note_pass "unrecognized regular marker was not sent"; fi
if grep -Fxq KEEP-PENDING-VICTIM "$WORK/pending-victim"; then
  note_pass "pending write did not follow its destination symlink"
else note_fail "$CASE: pending write changed the symlink target"; fi
if [ -f "$PENDING_FILE" ] && [ ! -L "$PENDING_FILE" ]; then
  note_pass "pending marker was atomically replaced with a regular file"
else note_fail "$CASE: pending marker did not replace the symlink safely"; fi
AGENT_MARKER_DIR=""
PENDING_FILE=""

# ===========================================================================
# CASE 8 - everything succeeds.
# ===========================================================================
HANDOFF=full
run_case "everything succeeding"
expect_code 0
expect_line "Morning briefing: delivered"
expect_line "Decisions document: delivered"
expect_not_line "Calendar:"
expect_not_line "Tablet write check:"
expect_not_line "withheld"
expect_not_line "failed"
expect_sent
expect_outbound
if python3 - "$RUN_DIR/outcomes/$DATE.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["date"] == "2026-08-20"
assert data["send_status"] == "sent"
assert data["outcome"].startswith("Morning Status Check:")
PY
then note_pass "the sent outcome has a valid daily record"
else note_fail "$CASE: the daily outcome record is missing or invalid"; fi
# One line to a human: no paths, no ticket ids, no jargon.
if printf '%s' "$LINE" | grep -qE '/tmp|/Users|\.sh|mc-[a-z0-9]{4}|rmapi|origin/main'; then
  note_fail "$CASE: the outcome line leaked a path, a tool name or a ticket id"
else note_pass "no paths, tool names or ticket ids in the outcome line"; fi
if [ "$(printf '%s' "$LINE" | wc -c)" -lt 220 ]; then note_pass "outcome line is phone-sized"
else note_fail "$CASE: outcome line is $(printf '%s' "$LINE" | wc -c) characters, too long for a phone"; fi
if [ "$(printf '%s\n' "$LINE" | wc -l | tr -d ' ')" = "3" ] \
  && printf '%s' "$LINE" | grep -q '^Morning Status Check:$'; then
  note_pass "outcome has a heading and two delivery lines, with healthy checks omitted"
else note_fail "$CASE: outcome broke the labelled exception-only contract"; fi

# ===========================================================================
# CASE 9 - dry run. An outcome line, and provably nothing leaves the script.
# ===========================================================================
HANDOFF=full
ALLOW_OUTBOUND=0
run_case "dry run" MB_DRY_RUN=1
expect_code 0
expect_line "Tablet write check: skipped"
expect_no_outbound
if [ -n "$(find "$RUN_DIR/device" -type f 2>/dev/null)" ]; then note_fail "$CASE: a dry run wrote to the device"
else note_pass "dry run wrote nothing to the device"; fi
if [ -e "$RUN_DIR/outcomes/$DATE.json" ] || [ -e "$RUN_DIR/pending" ]; then
  note_fail "$CASE: a dry run created delivery evidence"
else note_pass "dry run created no delivery evidence"; fi

# ===========================================================================
# CASE 10 - THE GUARD ITSELF. No flag at all, no dry-run flag either: the
# default for an unset flag is DO NOT SEND. This is the case that would have
# stopped the 02:48 bus messages. The outcome line is still produced, and the
# run does NOT look like a clean morning.
# ===========================================================================
HANDOFF=full
ALLOW_OUTBOUND=0
run_case "outbound off by default"
expect_no_outbound
if [ -n "$LINE" ]; then note_pass "the outcome line is still produced with outbound off"
else note_fail "$CASE: no outcome line was produced"; fi
expect_code 1
if grep -q 'OUTBOUND DISABLED' "$LOG"; then note_pass "the log says plainly that nothing was delivered"
else note_fail "$CASE: a run that delivered nothing did not say so"; fi
if grep -q 'OUTBOUND SUPPRESSED' "$LOG"; then note_pass "each suppressed action is named in the log"
else note_fail "$CASE: suppressed actions were not logged"; fi
ALLOW_OUTBOUND=1

# ===========================================================================
# MUTATION CONTROL. If the decoupling is ever removed - if the briefing branch
# goes back to exiting the script - case 1 must stop passing. Prove the suite
# can see that, by running a copy of the script with the old short-circuit
# spliced back in and REQUIRING the decisions step to be missed.
# ===========================================================================
echo
echo "CASE: mutation control (old short-circuit restored must be caught)"
MUT="$WORK/mutant.sh"
# shellcheck disable=SC2016  # the literal $HARD is the point: this is a sed pattern
sed 's|^    bump "\$HARD"$|    bump "$HARD"; exit 1|' "$SCRIPT" > "$MUT"
chmod +x "$MUT"
# shellcheck disable=SC2016
if ! grep -q 'bump "\$HARD"; exit 1' "$MUT"; then
  note_fail "mutation control: could not splice the old short-circuit back in"
else
  SCRIPT_REAL="$SCRIPT"; SCRIPT="$MUT"
  HANDOFF=none
  run_case "mutant no handoff"
  SCRIPT="$SCRIPT_REAL"
  MUT_SENT=$(count_lines "$RUN_DIR/imessage.log")
  if grep -q '^OUTCOME LINE: ' "$LOG" || [ "$MUT_SENT" != "0" ]; then
    note_fail "mutation control: the mutant still reached the reader - these tests are not binding"
  else
    note_pass "mutation control: with the old short-circuit back, the reader is told nothing, and the suite sees it"
  fi
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL CASES PASS"
  exit 0
fi
echo "$FAILURES CHECK(S) FAILED"
exit 1
