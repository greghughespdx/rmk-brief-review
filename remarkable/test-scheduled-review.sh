#!/usr/bin/env bash
# scheduled-review.sh, end to end, against fakes (mc-mi8xx).
#
# The renderer (push-decisions.sh) and the independent reviewer (codex exec)
# are both stubbed: neither is something this test may exercise for real
# (nothing here calls a real headless Claude session, and nothing here
# reaches reportlab or the device). What is NOT stubbed is the gate itself:
# review-receipt.py runs for real, so a pass here means the real hash-bound
# gate actually enforced what the fake reviewer wrote, exactly the way
# test-decisions-loop.sh keeps its gates real against a fixture device.
#
# The fake reviewer ("codex") writes its receipt through the SAME
# review-receipt.py write command scheduled-review.sh instructs a real
# reviewer to run, so the two-step design (reviewer writes, script checks)
# is exercised for real end to end.
#
# Isolation coverage (mc-mi8xx hardening, the cross-reviewer's MISMATCH review): the
# fake reviewer is given the STAGED pdf/manifest/receipt paths scheduled-
# review.sh actually tells a reviewer to use (mirrored here from the same
# $TMP/scheduled-review-stage-$DATE naming scheme the real script uses), and
# can be told to tamper with the staged copy, the real archive PDF, or the
# real archive manifest, or to exit nonzero after writing an otherwise-good
# receipt, or to run past a timeout. Every one of those must end in failure
# with nothing published to the archive.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/scheduled-review.sh"
RECEIPT_TOOL="$HERE/review-receipt.py"
PYTHON_BIN="$(command -v python3)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ARCHIVE="$WORK/archive"
BIN="$WORK/bin"
FAKEHOME="$WORK/fakehome"
CALL_LOG="$WORK/calls.log"
LOG_FILE="$WORK/scheduled-review.log"
FAIL=0
# Keep ordinary scenarios independent of the wall clock. Only the cutoff
# scenarios below override FAKE_NOW_EPOCH with their boundary timestamps.
DEFAULT_NOW_EPOCH="$(date -j -f "%Y-%m-%d %H:%M" "$(date +%F) 01:00" +%s)"

mkdir -p "$ARCHIVE" "$BIN" "$FAKEHOME"

ok() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }
note() { echo; echo "--- $* ---"; }

# --- fakes on PATH-independent absolute paths --------------------------------
#
# verify-queue.py: the real one reaches GitHub and live services
# (test-decisions-loop.sh stubs it for the same reason). Always reports one
# verified row and exits 0, matching the real contract ("exit 0 always").
cat > "$BIN/verify-queue.py" <<'PY'
import os, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
with open(os.environ["CALL_LOG"], "a") as f:
    f.write("verify-queue " + " ".join(sys.argv[1:]) + "\n")
open(args["--out-queue"], "w").write("| row | ask |\n")
open(args["--report"], "w").write('{"rows":[{"item_id":"row","verified":true,"held_reason":""}]}')
PY

# push-decisions.sh: the renderer. Records every call (so a test can prove
# --push was never passed) and, on FAKE_RENDER_RC=0, writes a PDF and
# manifest with FAKE_CARDS card ids at the exact path scheduled-review.sh
# expects (decisions-DATE.pdf / .manifest.json under ARCHIVE_ROOT/DATE).
cat > "$BIN/push-decisions.sh" <<'SH'
#!/usr/bin/env bash
echo "push-decisions $*" >> "$CALL_LOG"
date_arg=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --push) echo "push-decisions: --push must never be passed by scheduled-review.sh" >&2; exit 9 ;;
    --date) date_arg="$2"; shift 2 ;;
    *) shift ;;
  esac
done
rc="${FAKE_RENDER_RC:-0}"
[ "$rc" -eq 0 ] || exit "$rc"
outdir="$ARCHIVE_ROOT/$date_arg"
mkdir -p "$outdir"
printf 'FAKE-PDF-BYTES-%s\n' "$date_arg" > "$outdir/decisions-$date_arg.pdf"
cards="${FAKE_CARDS:-card-a card-b}"
cardjson=""
for c in $cards; do cardjson="$cardjson{\"card_id\":\"$c\",\"headline\":\"headline for $c\"},"; done
printf '{"page_id":"page-%s","manifest_hash":"mh-%s","cards":[%s]}\n' \
  "$date_arg" "$date_arg" "${cardjson%,}" > "$outdir/decisions-$date_arg.manifest.json"
echo "Generated locally. Add --push to send and register it."
exit 0
SH

# bus-alert.sh: records every call, never reaches the real bus.
cat > "$BIN/bus-alert.sh" <<'SH'
#!/usr/bin/env bash
echo "bus-alert $*" >> "$CALL_LOG"
exit 0
SH

# codex: the independent reviewer. scheduled-review.sh invokes it directly;
# like the real one it gets HOME from the environment, and this fake reads
# its instructions from a control file under HOME rather than from
# environment variables set by this test. It writes its verdicts through the
# real review-receipt.py, exactly as the real reviewer is instructed to, and
# can be told (via the control file) to tamper with a file, sleep past a
# timeout, ignore SIGTERM, or exit nonzero - all needed to exercise the
# hardening in scheduled-review.sh.
cat > "$BIN/codex" <<'SH'
#!/usr/bin/env bash
# shellcheck disable=SC1090
. "$HOME/control.env"
echo "codex-called $*" >> "$CALL_LOG"
# The real codex binary interprets -C DIR itself (chdir + sandbox root); this
# fake is not the real binary, so it has to do that part itself to prove
# scheduled-review.sh actually passed the staging dir, not the archive dir.
prev=""
cd_target=""
for a in "$@"; do
  if [ "$prev" = "-C" ]; then cd_target="$a"; fi
  prev="$a"
done
[ -n "$cd_target" ] && cd "$cd_target"
echo "codex-pwd $(pwd)" >> "$CALL_LOG"

# mc-vg49y: a per-run attempt counter (under HOME, reset by the test
# harness's run_scheduled_review before every run) so control.env can give
# the FIRST launch attempt different behavior from a relaunch, via
# <VAR>_<attempt> overrides. Every existing scenario that never sets an
# _<N> override is unaffected: the plain <VAR> applies to every attempt,
# exactly as before this counter existed.
COUNTER_FILE="$HOME/.codex-attempt-counter"
ATTEMPT=1
if [ -f "$COUNTER_FILE" ]; then
  ATTEMPT=$(( $(cat "$COUNTER_FILE") + 1 ))
fi
echo "$ATTEMPT" > "$COUNTER_FILE"
echo "codex-attempt $ATTEMPT" >> "$CALL_LOG"

attempt_var() {  # attempt_var <NAME> - prints ${NAME}_${ATTEMPT} if set and
                  # non-empty, else plain ${NAME} (possibly empty).
  local base="$1" indexed="${1}_${ATTEMPT}" value
  eval "value=\"\${$indexed:-}\""
  if [ -n "$value" ]; then printf '%s' "$value"; return; fi
  eval "value=\"\${$base:-}\""
  printf '%s' "$value"
}

SLEEP_SECONDS="$(attempt_var FAKE_SLEEP_SECONDS)"
IGNORE_TERM="$(attempt_var FAKE_IGNORE_TERM)"
MUTATE_PATH="$(attempt_var FAKE_MUTATE_PATH)"
MUTATE_MANIFEST_FIELD="$(attempt_var FAKE_MUTATE_MANIFEST_FIELD)"
SKIP_WRITE="$(attempt_var FAKE_SKIP_WRITE)"
EXIT_CODE="$(attempt_var FAKE_EXIT_CODE)"
STARTUP_OUTPUT="$(attempt_var FAKE_STARTUP_OUTPUT)"
[ -z "$STARTUP_OUTPUT" ] || printf '%s\n' "$STARTUP_OUTPUT"
[ -n "$EXIT_CODE" ] || EXIT_CODE=0

if [ -n "$IGNORE_TERM" ]; then trap '' TERM; fi
if [ -n "$SLEEP_SECONDS" ]; then sleep "$SLEEP_SECONDS"; fi
if [ -n "$MUTATE_PATH" ]; then rm -f "$MUTATE_PATH"; printf 'TAMPERED\n' > "$MUTATE_PATH"; fi
# Field-preserving manifest tamper: changes the manifest's bytes without
# touching its declared "manifest_hash" field. The receipt's file hash and
# scheduled-review.sh's before/after hash must each refuse this change.
if [ -n "$MUTATE_MANIFEST_FIELD" ]; then
  "$FAKE_PYTHON" -c '
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["tampered_field"] = True
json.dump(d, open(p, "w"))
' "$MUTATE_MANIFEST_FIELD"
fi
verdict_args=()
i=1
while [ "$i" -le "${FAKE_VERDICT_COUNT:-0}" ]; do
  eval "v=\"\${FAKE_VERDICT_$i}\""
  verdict_args+=(--verdict "$v")
  i=$((i + 1))
done
if [ -n "$SKIP_WRITE" ]; then exit "$EXIT_CODE"; fi
"$FAKE_PYTHON" "$FAKE_RECEIPT_TOOL" write --date "$FAKE_DATE" \
  --reviewer scheduled-codex-reviewer \
  --pdf "$FAKE_PDF" --manifest "$FAKE_MANIFEST" --receipt "$FAKE_RECEIPT" \
  "${verdict_args[@]}"
exit "$EXIT_CODE"
SH

cat > "$BIN/pdftoppm" <<'SH'
#!/usr/bin/env bash
# fake page renderer: pdftoppm -r N -png <pdf> <prefix>
prefix="${@: -1}"
printf 'png' > "${prefix}-1.png"
SH

chmod +x "$BIN/push-decisions.sh" "$BIN/bus-alert.sh" "$BIN/codex" "$BIN/pdftoppm"
: > "$BIN/prompt.md"

run_scheduled_review() {  # run_scheduled_review <date> [<script>] [<extra env assignment> ...]
  local date="$1"
  local script="${2:-$SCRIPT}"
  : > "$CALL_LOG"
  : > "$LOG_FILE"
  # mc-vg49y: a fresh attempt counter every run - the fake codex reads this
  # (under HOME) to tell a first launch attempt from a relaunch. Without
  # this reset the counter would carry over from a PRIOR scenario in this
  # same test file run (FAKEHOME is shared across all of them).
  rm -f "$FAKEHOME/.codex-attempt-counter"
  CALL_LOG="$CALL_LOG" \
  ARCHIVE_ROOT="$ARCHIVE" \
  HOME="$FAKEHOME" \
  REMARKABLE_TODAY_ARCHIVE="$ARCHIVE" \
  SCHEDULED_REVIEW_TMP="$WORK/tmp" \
  SCHEDULED_REVIEW_VERIFY_QUEUE="$BIN/verify-queue.py" \
  SCHEDULED_REVIEW_PUSH_DECISIONS="$BIN/push-decisions.sh" \
  SCHEDULED_REVIEW_RECEIPT_TOOL="$RECEIPT_TOOL" \
  SCHEDULED_REVIEW_BUS_ALERT="$BIN/bus-alert.sh" \
  SCHEDULED_REVIEW_CODEX="${FAKE_CODEX_BIN:-$BIN/codex}" \
  SCHEDULED_REVIEW_PDFTOPPM="$BIN/pdftoppm" \
  SCHEDULED_REVIEW_PROMPT="$BIN/prompt.md" \
  SCHEDULED_REVIEW_PYTHON="$PYTHON_BIN" \
  SCHEDULED_REVIEW_LOCK="$WORK/lock.dir" \
  SCHEDULED_REVIEW_LOG="$LOG_FILE" \
  SCHEDULED_REVIEW_TIMEOUT_SECONDS="${FAKE_TIMEOUT_SECONDS:-2400}" \
  SCHEDULED_REVIEW_KILL_GRACE_SECONDS="${FAKE_KILL_GRACE_SECONDS:-15}" \
  SCHEDULED_REVIEW_CUTOFF="${FAKE_CUTOFF:-05:40}" \
  SCHEDULED_REVIEW_HANDOFF="${FAKE_HANDOFF:-06:00}" \
  SCHEDULED_REVIEW_NOW_EPOCH="${FAKE_NOW_EPOCH:-$DEFAULT_NOW_EPOCH}" \
  SCHEDULED_REVIEW_LAUNCH_NOW_EPOCH="${FAKE_LAUNCH_NOW_EPOCH:-}" \
  SCHEDULED_REVIEW_SHASUM="${FAKE_SHASUM:-shasum}" \
  SCHEDULED_REVIEW_REVIEWER_KEEP_PATH="${FAKE_REVIEWER_KEEP_PATH:-}" \
  SCHEDULED_REVIEW_REVIEWER_COPY_ROOT="${FAKE_REVIEWER_COPY_ROOT:-$WORK/reviewer-copy-root}" \
  SCHEDULED_REVIEW_LAUNCH_WATCH_SECONDS="${FAKE_LAUNCH_WATCH_SECONDS:-100000}" \
  SCHEDULED_REVIEW_STUCK_RSS_KB="${FAKE_STUCK_RSS_KB:-1024}" \
  SCHEDULED_REVIEW_STUCK_PROBE="${FAKE_STUCK_PROBE:-}" \
  SCHEDULED_REVIEW_COPY_MIN_REMAINING_SECONDS="${FAKE_COPY_MIN_REMAINING_SECONDS:-600}" \
  SCHEDULED_REVIEW_POST_COPY_NOW_EPOCH="${FAKE_POST_COPY_NOW_EPOCH:-}" \
  FAKE_RENDER_RC="${FAKE_RENDER_RC:-0}" \
  FAKE_CARDS="${FAKE_CARDS:-card-a card-b}" \
    "$script" --date "$date" > "$WORK/out" 2> "$WORK/err"
  RC=$?
  mkdir -p "$WORK/tmp"
}

# stage_paths <date> - echo the STAGE_PDF/STAGE_MANIFEST/STAGE_RECEIPT paths
# the real script will use for this date, given SCHEDULED_REVIEW_TMP=$WORK/tmp.
# Mirrors scheduled-review.sh's own STAGE_DIR="$TMP/scheduled-review-stage-$DATE".
stage_pdf() { echo "$WORK/tmp/scheduled-review-stage-$1/decisions-$1.pdf"; }
stage_manifest() { echo "$WORK/tmp/scheduled-review-stage-$1/decisions-$1.manifest.json"; }
stage_receipt() { echo "$WORK/tmp/scheduled-review-stage-$1/review-receipt.json"; }

write_control() {  # write_control <date> <pdf> <manifest> <receipt> [<verdict>...]
  # Verdict notes carry spaces ("missing context for card-b"), so verdicts are
  # passed as separate arguments and %q-escaped into the control file rather
  # than space-joined into one string the fake codex would word-split wrong.
  local date="$1" pdf="$2" manifest="$3" receipt="$4"
  shift 4
  {
    printf 'FAKE_PYTHON=%q\n' "$PYTHON_BIN"
    printf 'FAKE_RECEIPT_TOOL=%q\n' "$RECEIPT_TOOL"
    printf 'FAKE_DATE=%q\n' "$date"
    printf 'FAKE_PDF=%q\n' "$pdf"
    printf 'FAKE_MANIFEST=%q\n' "$manifest"
    printf 'FAKE_RECEIPT=%q\n' "$receipt"
    printf 'CALL_LOG=%q\n' "$CALL_LOG"
    printf 'FAKE_VERDICT_COUNT=%d\n' "$#"
    local i=1
    for v in "$@"; do
      printf 'FAKE_VERDICT_%d=%q\n' "$i" "$v"
      i=$((i + 1))
    done
  } > "$FAKEHOME/control.env"
}

# write_control_for <date> [<verdict>...] - convenience wrapper using this
# date's own STAGED paths (the paths the real script actually hands the
# reviewer), which is what every scenario below except the raw-path ones needs.
write_control_for() {
  local date="$1"; shift
  write_control "$date" "$(stage_pdf "$date")" "$(stage_manifest "$date")" "$(stage_receipt "$date")" "$@"
}

set_control_var() {  # set_control_var <NAME> <value> - append to control.env
  printf '%s=%q\n' "$1" "$2" >> "$FAKEHOME/control.env"
}

calls_matching() { grep -c "^$1" "$CALL_LOG" 2>/dev/null || true; }

mkdir -p "$WORK/tmp"
TEST_PID="$$"

# =============================================================================
note "already-reviewed: a valid receipt is left alone, no fakes are touched"
DATE1="2026-09-01"
OUT1="$ARCHIVE/$DATE1"
mkdir -p "$OUT1"
printf 'PDF-BYTES-1\n' > "$OUT1/decisions-$DATE1.pdf"
printf '{"page_id":"p1","manifest_hash":"mh1","cards":[{"card_id":"card-a","headline":"h"}]}\n' \
  > "$OUT1/decisions-$DATE1.manifest.json"
"$PYTHON_BIN" "$RECEIPT_TOOL" write --date "$DATE1" --reviewer scheduled-codex-reviewer \
  --pdf "$OUT1/decisions-$DATE1.pdf" --manifest "$OUT1/decisions-$DATE1.manifest.json" \
  --receipt "$OUT1/review-receipt.json" --verdict "card-a:yes,yes,yes" >/dev/null
write_control_for "$DATE1"
run_scheduled_review "$DATE1"
ok test "$RC" -eq 0
ok grep -q "already valid" "$LOG_FILE"
ok test "$(calls_matching 'verify-queue ')" -eq 0
ok test "$(calls_matching 'push-decisions ')" -eq 0
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 0

# =============================================================================
note "all-yes verdicts: the receipt passes check, script exits 0"
DATE2="2026-09-02"
OUT2="$ARCHIVE/$DATE2"
PDF2="$OUT2/decisions-$DATE2.pdf"
MANIFEST2="$OUT2/decisions-$DATE2.manifest.json"
RECEIPT2="$OUT2/review-receipt.json"
write_control_for "$DATE2" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE2"
ok test "$RC" -eq 0
ok grep -q "review receipt VALID" "$LOG_FILE"
ok test "$(calls_matching 'verify-queue ')" -eq 1
ok test "$(calls_matching 'push-decisions ')" -eq 1
ok test "$(calls_matching 'codex-called ')" -eq 1
ok test "$(calls_matching 'bus-alert ')" -eq 0
ok test -f "$RECEIPT2"
ok "$PYTHON_BIN" "$RECEIPT_TOOL" --archive-root "$ARCHIVE" check \
  --date "$DATE2" --pdf "$PDF2" --manifest "$MANIFEST2" --receipt "$RECEIPT2" --producer orchestrator
# Isolation: codex ran with its cwd inside the staging dir, never the archive.
EXPECT_STAGE_PWD="$WORK/tmp/scheduled-review-stage-$DATE2"
ok grep -qF "codex-pwd $EXPECT_STAGE_PWD" "$CALL_LOG"
ok bash -c "! grep -qF 'codex-pwd $OUT2' '$CALL_LOG'"

# =============================================================================
note "one no verdict: nothing published to the archive, bus alert attempted, exit nonzero"
DATE3="2026-09-03"
OUT3="$ARCHIVE/$DATE3"
PDF3="$OUT3/decisions-$DATE3.pdf"
MANIFEST3="$OUT3/decisions-$DATE3.manifest.json"
RECEIPT3="$OUT3/review-receipt.json"
write_control_for "$DATE3" "card-a:yes,yes,yes" "card-b:no,yes,yes:missing context for card-b"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE3"
ok test "$RC" -ne 0
ok grep -q "FAILURE" "$LOG_FILE"
ok test ! -f "$RECEIPT3"
ok test -z "$(find "$OUT3" -maxdepth 1 -name 'review-receipt*.json' -print -quit)"
ok test "$(calls_matching 'bus-alert ')" -eq 1
ok grep -q "card-b" "$CALL_LOG"
ok grep -q "missing context for card-b" "$CALL_LOG"

# =============================================================================
note "render GENERATION FAILED (rc=2): no reviewer call, alert sent"
DATE4="2026-09-04"
write_control_for "$DATE4"
FAKE_RENDER_RC=2 run_scheduled_review "$DATE4"
ok test "$RC" -ne 0
ok grep -q "GENERATION FAILED" "$LOG_FILE"
ok test "$(calls_matching 'push-decisions ')" -eq 1
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "render EMPTY-DOC TRIPWIRE (rc=3): no reviewer call, alert sent"
DATE4b="2026-09-16"
write_control_for "$DATE4b"
FAKE_RENDER_RC=3 run_scheduled_review "$DATE4b"
ok test "$RC" -ne 0
ok grep -q "EMPTY-DOC TRIPWIRE" "$LOG_FILE"
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "render nothing-to-ask (rc=5, zero cards): not a failure, exits 0, no alert"
DATE5="2026-09-05"
write_control_for "$DATE5"
FAKE_RENDER_RC=5 run_scheduled_review "$DATE5"
ok test "$RC" -eq 0
ok grep -q "nothing to ask today" "$LOG_FILE"
ok test "$(calls_matching 'push-decisions ')" -eq 1
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 0

# =============================================================================
note "B6: a malformed --date is usage exit 2, nothing removed or touched"
BAD1="not-a-date"
run_scheduled_review "$BAD1"
ok test "$RC" -eq 2
ok test "$(calls_matching 'verify-queue ')" -eq 0
ok test "$(calls_matching 'push-decisions ')" -eq 0
ok test ! -d "$WORK/lock.dir"

BAD2="2026-9-3"
run_scheduled_review "$BAD2"
ok test "$RC" -eq 2

# =============================================================================
note "B5: a lock held by a LIVE pid is never taken over"
LIVE_LOCK="$WORK/lock.dir"
rm -rf "$LIVE_LOCK"
mkdir -p "$LIVE_LOCK"
echo "$TEST_PID" > "$LIVE_LOCK/pid"
DATE6="2026-09-06"
write_control_for "$DATE6"
run_scheduled_review "$DATE6"
ok test "$RC" -ne 0
ok grep -q "another run holds" "$LOG_FILE"
ok test -d "$LIVE_LOCK"
ok grep -qx "$TEST_PID" "$LIVE_LOCK/pid"
ok test "$(calls_matching 'codex-called ')" -eq 0
rm -rf "$LIVE_LOCK"

note "B5: a lock held by a DEAD pid is taken over, run proceeds"
( exit 0 ) &
DEAD_PID=$!
wait "$DEAD_PID" 2>/dev/null
mkdir -p "$WORK/lock.dir"
echo "$DEAD_PID" > "$WORK/lock.dir/pid"
DATE7="2026-09-07"
write_control_for "$DATE7" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE7"
ok test "$RC" -eq 0
ok grep -q "not alive.*removing and re-acquiring" "$LOG_FILE"
# The lock is fully released (directory and pid file both gone) once this
# run completes - a completed run must never leave a stale lock behind for
# the NEXT run to have to reason about.
ok test ! -e "$WORK/lock.dir/pid"
ok test ! -d "$WORK/lock.dir"

# =============================================================================
note "B1: the reviewer tampers with the STAGED pdf copy - fails, nothing published"
DATE8="2026-09-08"
OUT8="$ARCHIVE/$DATE8"
write_control_for "$DATE8" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_MUTATE_PATH "$(stage_pdf "$DATE8")"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE8"
ok test "$RC" -ne 0
ok test ! -f "$OUT8/review-receipt.json"
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "B1: the reviewer tampers with the REAL pdf (not the staged copy) - fails, nothing published"
DATE9="2026-09-09"
OUT9="$ARCHIVE/$DATE9"
write_control_for "$DATE9" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_MUTATE_PATH "$OUT9/decisions-$DATE9.pdf"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE9"
ok test "$RC" -ne 0
ok grep -q "real PDF" "$LOG_FILE"
ok test ! -f "$OUT9/review-receipt.json"
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "B1: the reviewer tampers with the REAL manifest - fails, nothing published"
DATE10="2026-09-10"
OUT10="$ARCHIVE/$DATE10"
write_control_for "$DATE10" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_MUTATE_PATH "$OUT10/decisions-$DATE10.manifest.json"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE10"
ok test "$RC" -ne 0
ok grep -q "real manifest" "$LOG_FILE"
ok test ! -f "$OUT10/review-receipt.json"
ok test "$(calls_matching 'bus-alert ')" -eq 1

# =============================================================================
note "reviewer exits nonzero despite an otherwise-good receipt: hard failure, nothing published"
DATE11="2026-09-11"
OUT11="$ARCHIVE/$DATE11"
write_control_for "$DATE11" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_EXIT_CODE 1
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE11"
ok test "$RC" -ne 0
ok grep -q "exited 1" "$LOG_FILE"
ok test ! -f "$OUT11/review-receipt.json"
ok test "$(calls_matching 'bus-alert ')" -eq 1

# =============================================================================
note "B2: the reviewer is killed after the timeout; script fails, nothing published"
DATE12="2026-09-12"
OUT12="$ARCHIVE/$DATE12"
write_control_for "$DATE12" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 30
set_control_var FAKE_IGNORE_TERM 1
FAKE_TIMEOUT_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE12"
ok test "$RC" -ne 0
ok grep -q "timed out" "$LOG_FILE"
ok test ! -f "$OUT12/review-receipt.json"
ok test "$(calls_matching 'bus-alert ')" -eq 1

# =============================================================================
note "cutoff/handoff: a run starting between cutoff and handoff refuses to start"
DATE13="2026-09-13"
write_control_for "$DATE13" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW13="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:50" +%s)"
FAKE_NOW_EPOCH="$NOW13" FAKE_CUTOFF="05:40" FAKE_HANDOFF="06:00" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE13"
ok test "$RC" -ne 0
ok grep -q "started too late" "$LOG_FILE"
ok test "$(calls_matching 'verify-queue ')" -eq 0
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "cutoff/handoff: the reviewer's time budget is clipped to the cutoff, not the full timeout"
DATE14="2026-09-14"
write_control_for "$DATE14" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 20
set_control_var FAKE_IGNORE_TERM 1
TODAY="$(date +%F)"
CUTOFF_EPOCH14="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:40" +%s)"
NOW14=$((CUTOFF_EPOCH14 - 2))
START14="$(date +%s)"
FAKE_NOW_EPOCH="$NOW14" FAKE_CUTOFF="05:40" FAKE_HANDOFF="06:00" \
  FAKE_TIMEOUT_SECONDS=2400 FAKE_KILL_GRACE_SECONDS=1 \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE14"
END14="$(date +%s)"
ok test "$RC" -ne 0
ok grep -q "timed out" "$LOG_FILE"
ELAPSED14=$((END14 - START14))
ok test "$ELAPSED14" -lt 15

note "stale stage receipt: a receipt left from an earlier run never counts as this run's review"
DATE17="2026-09-17"
write_control_for "$DATE17" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE17"
ok test "$RC" -eq 0
ok test -f "$ARCHIVE/$DATE17/review-receipt.json"
mkdir -p "$(dirname "$(stage_receipt "$DATE17")")"
cp "$ARCHIVE/$DATE17/review-receipt.json" "$(stage_receipt "$DATE17")"
rm -f "$ARCHIVE/$DATE17/review-receipt.json"
set_control_var FAKE_SKIP_WRITE 1
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE17"
ok test "$RC" -ne 0
ok grep -q "wrote no receipt" "$LOG_FILE"
ok test ! -f "$ARCHIVE/$DATE17/review-receipt.json"

note "mutation: without clearing the staging dir, the stale receipt would be published"
MUT17="$WORK/mutant-stale-stage.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUT17" <<'PY'
import sys
src,dst=sys.argv[1],sys.argv[2]; t=open(src).read()
n='rm -rf "$STAGE_DIR" 2>/dev/null\nif [ -e "$STAGE_DIR" ]; then\n  fail "could not clear the old staging dir $STAGE_DIR"\nfi\nmkdir "$STAGE_DIR" || fail "could not create staging dir $STAGE_DIR"\n'
assert n in t, "stale-stage mutation target not found"
t=t.replace(n,'mkdir -p "$STAGE_DIR"\n',1)
t=t.replace('mkdir "$STAGE_PAGES" || fail','mkdir -p "$STAGE_PAGES" || fail',1)
t=t.replace('cp "$PDF" "$STAGE_PDF" ||','cp -f "$PDF" "$STAGE_PDF" 2>/dev/null; true ||',1)
t=t.replace('cp "$MANIFEST" "$STAGE_MANIFEST" ||','cp -f "$MANIFEST" "$STAGE_MANIFEST" 2>/dev/null; true ||',1)
t=t.replace('chmod 444 "$STAGE_PDF" "$STAGE_MANIFEST" || fail','chmod 444 "$STAGE_PDF" "$STAGE_MANIFEST" 2>/dev/null; true || fail',1)
open(dst,'w').write(t)
PY
chmod +x "$MUT17"
mkdir -p "$(dirname "$(stage_receipt "$DATE17")")"
chmod u+w "$(stage_pdf "$DATE17")" "$(stage_manifest "$DATE17")" 2>/dev/null || true
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE17"
cp "$ARCHIVE/$DATE17/review-receipt.json" "$WORK/receipt17.json" 2>/dev/null || true
set_control_var FAKE_SKIP_WRITE ""
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE17"
mkdir -p "$(dirname "$(stage_receipt "$DATE17")")"
cp "$ARCHIVE/$DATE17/review-receipt.json" "$(stage_receipt "$DATE17")"
rm -f "$ARCHIVE/$DATE17/review-receipt.json"
set_control_var FAKE_SKIP_WRITE 1
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE17" "$MUT17"
ok test -f "$ARCHIVE/$DATE17/review-receipt.json"
set_control_var FAKE_SKIP_WRITE ""

note "launch-time budget: a render that finishes after the cutoff never starts the reviewer"
DATE15="2026-09-15"
write_control_for "$DATE15" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW15="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:00" +%s)"
LAUNCH15="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:45" +%s)"
FAKE_NOW_EPOCH="$NOW15" FAKE_LAUNCH_NOW_EPOCH="$LAUNCH15" FAKE_CUTOFF="05:40" FAKE_HANDOFF="06:00" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE15"
ok test "$RC" -ne 0
ok grep -q "render finished after the 05:40 cutoff" "$LOG_FILE"
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test ! -f "$ARCHIVE/$DATE15/review-receipt.json"
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "integrity: a failing hash command is a hard failure, never an empty-equals-empty pass"
DATE16="2026-09-16"
write_control_for "$DATE16" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
printf '#!/usr/bin/env bash\nexit 1\n' > "$BIN/shasum-broken"
printf '#!/usr/bin/env bash\necho "not-a-digest  $3"\n' > "$BIN/shasum-garbage"
chmod +x "$BIN/shasum-broken" "$BIN/shasum-garbage"
for fake in shasum-broken shasum-garbage; do
  FAKE_SHASUM="$BIN/$fake" FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE16"
  ok test "$RC" -ne 0
  ok grep -q "could not hash the real PDF" "$LOG_FILE"
  ok test "$(calls_matching 'codex-called ')" -eq 0
  ok test ! -f "$ARCHIVE/$DATE16/review-receipt.json"
done

note "mutation: without the launch-time recheck, the late-render case would start the reviewer"
MUT15="$WORK/mutant-launch.sh"
sed 's/if \[ "$LAUNCH_NOW_EPOCH" -ge "$CUTOFF_EPOCH" \]; then/if false; then/' "$SCRIPT" > "$MUT15"; chmod +x "$MUT15"
ok grep -q 'if false; then' "$MUT15"
FAKE_NOW_EPOCH="$NOW15" FAKE_LAUNCH_NOW_EPOCH="$LAUNCH15" FAKE_CUTOFF="05:40" FAKE_HANDOFF="06:00" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE15" "$MUT15"
# Without the recheck the reviewer is launched with an overdue, negative budget.
ok grep -q "reviewer budget -" "$LOG_FILE"

note "mutation: without digest validation, a garbage hash command lets the review run"
MUT16="$WORK/mutant-hash.sh"
sed "s/  printf '%s' \"\$digest\" | grep -Eq '\^\[0-9a-f\]{64}\$' || return 1/  true/" "$SCRIPT" > "$MUT16"; chmod +x "$MUT16"
ok grep -q '^  true$' "$MUT16"
FAKE_SHASUM="$BIN/shasum-garbage" FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE16" "$MUT16"
ok test "$(calls_matching 'codex-called ')" -ge 1

# =============================================================================
# mc-0kpzk: the reviewer log is kept on a reviewer-related failure, mode
# 0600, with its path printed in the log; reviewer start/finish/elapsed are
# logged on every run, pass or fail (origin: two back-to-back timeouts on
# 2026-09-28 left no evidence of where the reviewer's time went, because its
# transcript was gone by the time anyone looked).
reviewer_keep_path() { echo "$WORK/tmp/scheduled-review-$1-reviewer.log"; }
raw_reviewer_log_path() { echo "$WORK/tmp/scheduled-review-reviewer-$1.log"; }
file_mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1" 2>/dev/null; }

note "mc-0kpzk: a timed-out reviewer's log is kept (0600) and the timing line is logged"
DATE18="2026-09-18"
KEPT18="$(reviewer_keep_path "$DATE18")"
rm -f "$KEPT18"
write_control_for "$DATE18" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 30
set_control_var FAKE_IGNORE_TERM 1
FAKE_TIMEOUT_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE18"
ok test "$RC" -ne 0
ok grep -q "timed out" "$LOG_FILE"
ok test -f "$KEPT18"
ok test "$(file_mode "$KEPT18")" = "600"
# The fake reviewer is killed before it emits anything to stdout (its
# "codex-called"/"codex-pwd" markers go straight to CALL_LOG, not stdout),
# so the kept file's existence and permissions are what this case proves -
# a real codex would leave a genuinely non-empty transcript.
ok grep -q "reviewer log kept at $KEPT18" "$LOG_FILE"
ok grep -Eq "reviewer started .* finished .* elapsed [0-9]+s \(budget .*timed_out=1, rc=" "$LOG_FILE"

note "mc-0kpzk: a nonzero reviewer exit also keeps the log"
DATE19="2026-09-19"
KEPT19="$(reviewer_keep_path "$DATE19")"
rm -f "$KEPT19"
write_control_for "$DATE19" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_EXIT_CODE 1
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE19"
ok test "$RC" -ne 0
ok grep -q "exited 1" "$LOG_FILE"
ok test -f "$KEPT19"
ok test "$(file_mode "$KEPT19")" = "600"
ok test -s "$KEPT19"
ok grep -q "review-receipt.json" "$KEPT19"
ok grep -q "reviewer log kept at $KEPT19" "$LOG_FILE"
ok grep -Eq "reviewer started .* finished .* elapsed [0-9]+s \(budget .*timed_out=0, rc=1\)" "$LOG_FILE"
# The cross-reviewer's review of bd3f1b21: the RAW reviewer log (the shell redirect
# target, before any keeping happens) must be private from creation, not
# just the kept copy - a plain `>"$REVIEWER_LOG"` redirect under the
# common 022 umask would create it 0644.
ok test "$(file_mode "$(raw_reviewer_log_path "$DATE19")")" = "600"

note "mc-0kpzk: a passing run logs the timing line but keeps no reviewer log"
DATE20="2026-09-20"
KEPT20="$(reviewer_keep_path "$DATE20")"
rm -f "$KEPT20"
write_control_for "$DATE20" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE20"
ok test "$RC" -eq 0
ok grep -Eq "reviewer started .* finished .* elapsed [0-9]+s \(budget .*timed_out=0, rc=0\)" "$LOG_FILE"
ok test ! -f "$KEPT20"
ok bash -c "! grep -q 'reviewer log kept at' '$LOG_FILE'"
# The raw log is private at creation regardless of whether this run passes
# or fails - it is written by the SAME redirect either way.
ok test "$(file_mode "$(raw_reviewer_log_path "$DATE20")")" = "600"

note "mc-0kpzk: the raw reviewer log is created private under launchd's default umask (022), proven explicitly rather than relying on the ambient shell's umask"
DATE20U="2026-09-21"
write_control_for "$DATE20U" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
UMASK_BEFORE_TEST="$(umask)"
umask 022
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE20U"
umask "$UMASK_BEFORE_TEST"
ok test "$RC" -eq 0
ok test "$(file_mode "$(raw_reviewer_log_path "$DATE20U")")" = "600"

note "mc-0kpzk: keep_reviewer_log's failure path (KEEP_DIR unwritable) logs the failure, fails the run as normal, and leaves nothing behind"
DATE21="2026-09-22"
READONLY_KEEP="$WORK/readonly-keep"
mkdir -p "$READONLY_KEEP"
chmod 555 "$READONLY_KEEP"
UNWRITABLE_KEEP_PATH="$READONLY_KEEP/scheduled-review-$DATE21-reviewer.log"
write_control_for "$DATE21" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_EXIT_CODE 1
BEFORE_COUNT=$(find "$READONLY_KEEP" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
FAKE_REVIEWER_KEEP_PATH="$UNWRITABLE_KEEP_PATH" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE21"
AFTER_COUNT=$(find "$READONLY_KEEP" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
ok test "$RC" -ne 0
ok grep -q "exited 1" "$LOG_FILE"
ok grep -q "could not keep reviewer log at $UNWRITABLE_KEEP_PATH" "$LOG_FILE"
ok test ! -e "$UNWRITABLE_KEEP_PATH"
# Nothing - not even a leftover mktemp scratch file - was left behind in the
# unwritable target directory by the failed keep attempt.
ok test "$BEFORE_COUNT" -eq "$AFTER_COUNT"
chmod 755 "$READONLY_KEEP"
rm -rf "$READONLY_KEEP"

# =============================================================================
# mc-vg49y: the scheduled reviewer (codex exec) was twice observed stuck
# before main() on 2026-09-30. Part A mitigates the suspected cause (a
# Caskroom-installed, quarantined binary under Gatekeeper's path-state
# cache - upstream openai/codex#23802) with a private, xattr-free copy of
# the binary's whole package directory. Part B is defense in depth: a
# reviewer still alive but showing no sign of having started a fixed
# interval after launch is killed and relaunched exactly once.
# =============================================================================
note "mc-vg49y Part A: private copy built from a fake Caskroom package dir, xattrs stripped, hash verified"
CASKROOT="$WORK/opt-fake"
CASK_PKG="$CASKROOT/Caskroom/codex/9.9.9"
mkdir -p "$CASK_PKG/bin" "$CASK_PKG/codex-resources"
cp "$BIN/codex" "$CASK_PKG/bin/codex"
chmod +x "$CASK_PKG/bin/codex"
echo "resource-data" > "$CASK_PKG/codex-resources/data.txt"
mkdir -p "$CASKROOT/bin"
ln -sf "../Caskroom/codex/9.9.9/bin/codex" "$CASKROOT/bin/codex"
xattr -w com.apple.quarantine "0081;00000000;test;" "$CASK_PKG/bin/codex"
COPYROOT_A="$WORK/reviewer-copy-A"
rm -rf "$COPYROOT_A"
PRIVATE_BIN_A="$COPYROOT_A/9.9.9/bin/codex"

DATE_A1="2026-09-23"
write_control_for "$DATE_A1" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_CODEX_BIN="$CASKROOT/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_A" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A1"
ok test "$RC" -eq 0
ok test -x "$PRIVATE_BIN_A"
ok bash -c "! xattr -p com.apple.quarantine '$PRIVATE_BIN_A' >/dev/null 2>&1"
ok bash -c "[ \"\$(shasum -a 256 '$PRIVATE_BIN_A' | awk '{print \$1}')\" = \"\$(shasum -a 256 '$CASK_PKG/bin/codex' | awk '{print \$1}')\" ]"
ok test -f "$COPYROOT_A/9.9.9/codex-resources/data.txt"
ok grep -q "built and verified private copy $PRIVATE_BIN_A" "$LOG_FILE"

note "mc-vg49y Part A: reused on a second run (hash matches, no rebuild)"
DATE_A2="2026-09-24"
write_control_for "$DATE_A2" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_CODEX_BIN="$CASKROOT/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_A" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A2"
ok test "$RC" -eq 0
ok grep -q "reusing private copy $PRIVATE_BIN_A" "$LOG_FILE"

note "mc-vg49y Part A: copy failure (unwritable copy root) falls back to the original CODEX_BIN"
CASKROOT2="$WORK/opt-fake2"
CASK_PKG2="$CASKROOT2/Caskroom/codex/1.0.0"
mkdir -p "$CASK_PKG2/bin"
cp "$BIN/codex" "$CASK_PKG2/bin/codex"
chmod +x "$CASK_PKG2/bin/codex"
mkdir -p "$CASKROOT2/bin"
ln -sf "../Caskroom/codex/1.0.0/bin/codex" "$CASKROOT2/bin/codex"
READONLY_PARENT="$WORK/readonly-copy-parent"
mkdir -p "$READONLY_PARENT"
chmod 555 "$READONLY_PARENT"
COPYROOT_FAIL="$READONLY_PARENT/codex-reviewer"

DATE_A3="2026-09-25"
OUT_A3="$ARCHIVE/$DATE_A3"
write_control_for "$DATE_A3" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_CODEX_BIN="$CASKROOT2/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_FAIL" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A3"
ok test "$RC" -eq 0
ok test -f "$OUT_A3/review-receipt.json"
ok grep -q "could not create $COPYROOT_FAIL; using $CASKROOT2/bin/codex directly" "$LOG_FILE"
ok test ! -e "$COPYROOT_FAIL"
chmod 755 "$READONLY_PARENT"
rm -rf "$READONLY_PARENT"

note "mc-vg49y Part A (the cross-reviewer's cross-review of 502c0b60, finding 1): low remaining budget skips building a new copy, CODEX_BIN used directly"
CASKROOT3="$WORK/opt-fake3"
CASK_PKG3="$CASKROOT3/Caskroom/codex/2.0.0"
mkdir -p "$CASK_PKG3/bin"
cp "$BIN/codex" "$CASK_PKG3/bin/codex"
chmod +x "$CASK_PKG3/bin/codex"
mkdir -p "$CASKROOT3/bin"
ln -sf "../Caskroom/codex/2.0.0/bin/codex" "$CASKROOT3/bin/codex"
COPYROOT_LOWTIME="$WORK/reviewer-copy-lowtime"
rm -rf "$COPYROOT_LOWTIME"

DATE_A4="2026-09-29"
OUT_A4="$ARCHIVE/$DATE_A4"
write_control_for "$DATE_A4" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
# Default SCHEDULED_REVIEW_COPY_MIN_REMAINING_SECONDS is 600; a 100s budget
# (far from the cutoff, so the cutoff math never also clips it) is below
# that margin, so resolve_reviewer_bin must skip building and use CODEX_BIN
# directly - reusing an existing copy is a different code path, untested
# here on purpose (a fresh, empty copy root forces the build-or-skip branch).
FAKE_CODEX_BIN="$CASKROOT3/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_LOWTIME" \
  FAKE_TIMEOUT_SECONDS=100 \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A4"
ok test "$RC" -eq 0
ok test -f "$OUT_A4/review-receipt.json"
ok grep -q "only 100s of budget remain (< 600s margin); skipping a new private copy, using $CASKROOT3/bin/codex directly" "$LOG_FILE"
ok test ! -e "$COPYROOT_LOWTIME/2.0.0"

note "mc-vg49y Part A (the cross-reviewer's cross-review of 502c0b60, finding 1): a copy that finishes after the cutoff fails closed, nothing is launched"
CASKROOT4="$WORK/opt-fake4"
CASK_PKG4="$CASKROOT4/Caskroom/codex/4.0.0"
mkdir -p "$CASK_PKG4/bin"
cp "$BIN/codex" "$CASK_PKG4/bin/codex"
chmod +x "$CASK_PKG4/bin/codex"
mkdir -p "$CASKROOT4/bin"
ln -sf "../Caskroom/codex/4.0.0/bin/codex" "$CASKROOT4/bin/codex"
COPYROOT_SLOW="$WORK/reviewer-copy-slow"
rm -rf "$COPYROOT_SLOW"

DATE_A5="2026-09-30"
OUT_A5="$ARCHIVE/$DATE_A5"
write_control_for "$DATE_A5" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
CUTOFF_EPOCH_A5="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:40" +%s)"
POST_COPY_A5=$(( CUTOFF_EPOCH_A5 + 60 ))
# The PRE-copy budget is the plentiful default (comfortably above the 600s
# margin, well before the cutoff), so the build is actually ATTEMPTED and
# SUCCEEDS (this fixture's copy is instant) - this test is specifically
# about the recheck AFTER a copy finishes, simulated here by forcing "now"
# at that recheck point to already be past the cutoff, exactly as a real
# slow copy crossing the cutoff would leave it.
FAKE_CODEX_BIN="$CASKROOT4/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_SLOW" \
  FAKE_POST_COPY_NOW_EPOCH="$POST_COPY_A5" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A5"
ok test "$RC" -ne 0
ok grep -q "resolving the reviewer binary finished after the 05:40 cutoff" "$LOG_FILE"
ok test ! -f "$OUT_A5/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 1

# =============================================================================
# The cross-reviewer's re-review of c08a627a: the two cutoff rechecks above were gated
# on "fresh now < HANDOFF_EPOCH", not on whether the RUN ITSELF started as a
# scheduled run. A scheduled run (started before the cutoff) that overran
# all the way PAST handoff read as if it were a manual daytime run under
# that old gate, skipped the cutoff fail entirely, and would have launched
# codex during or after the 06:00 push. These three scenarios exercise
# exactly that blind spot (fresh now >= HANDOFF_EPOCH, not just >=
# CUTOFF_EPOCH) at both recheck points, plus the manual-run carve-out that
# must keep working.
# =============================================================================
note "mc-vg49y (the cross-reviewer's re-review of c08a627a): pre-copy check - a scheduled run (started 05:00) whose render finishes at 06:01, PAST handoff, still fails closed"
DATE_PRE_HO="2026-11-10"
OUT_PRE_HO="$ARCHIVE/$DATE_PRE_HO"
write_control_for "$DATE_PRE_HO" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW_PRE_HO="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:00" +%s)"
LAUNCH_PRE_HO="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 06:01" +%s)"
FAKE_NOW_EPOCH="$NOW_PRE_HO" FAKE_LAUNCH_NOW_EPOCH="$LAUNCH_PRE_HO" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_PRE_HO"
ok test "$RC" -ne 0
ok grep -q "the render finished after the 05:40 cutoff" "$LOG_FILE"
ok test ! -f "$OUT_PRE_HO/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "mc-vg49y (the cross-reviewer's re-review of c08a627a): post-copy check - a scheduled run (started 05:00) whose copy resolution finishes at 06:01, PAST handoff, still fails closed"
DATE_POST_HO="2026-11-11"
OUT_POST_HO="$ARCHIVE/$DATE_POST_HO"
CASKROOT_HO="$WORK/opt-fake-ho"
CASK_PKG_HO="$CASKROOT_HO/Caskroom/codex/6.0.0"
mkdir -p "$CASK_PKG_HO/bin"
cp "$BIN/codex" "$CASK_PKG_HO/bin/codex"
chmod +x "$CASK_PKG_HO/bin/codex"
mkdir -p "$CASKROOT_HO/bin"
ln -sf "../Caskroom/codex/6.0.0/bin/codex" "$CASKROOT_HO/bin/codex"
COPYROOT_HO="$WORK/reviewer-copy-ho"
rm -rf "$COPYROOT_HO"
write_control_for "$DATE_POST_HO" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW_POST_HO="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:00" +%s)"
POST_COPY_HO="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 06:01" +%s)"
# PRE-copy budget is plentiful (05:00, well before the 05:40 cutoff) so the
# build is actually attempted and succeeds; only the POST-copy recheck is
# forced to 06:01 to simulate a copy that genuinely ran that long.
FAKE_NOW_EPOCH="$NOW_POST_HO" FAKE_CODEX_BIN="$CASKROOT_HO/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_HO" \
  FAKE_POST_COPY_NOW_EPOCH="$POST_COPY_HO" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_POST_HO"
ok test "$RC" -ne 0
ok grep -q "resolving the reviewer binary finished after the 05:40 cutoff" "$LOG_FILE"
ok test ! -f "$OUT_POST_HO/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 0
ok test "$(calls_matching 'bus-alert ')" -eq 1

note "mc-vg49y (the cross-reviewer's re-review of c08a627a): a genuinely MANUAL run starting at 06:30 (after handoff) is unaffected - relative timeout only, no cutoff gating, runs normally"
DATE_MANUAL="2026-11-12"
OUT_MANUAL="$ARCHIVE/$DATE_MANUAL"
write_control_for "$DATE_MANUAL" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW_MANUAL="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 06:30" +%s)"
FAKE_NOW_EPOCH="$NOW_MANUAL" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_MANUAL"
ok test "$RC" -eq 0
ok test -f "$OUT_MANUAL/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 1

note "MUTATION 10 (mc-vg49y, the cross-reviewer's re-review of c08a627a): reverting the post-copy gate to the old 'fresh now < HANDOFF_EPOCH' check (instead of IS_SCHEDULED_RUN) flips the past-handoff case from failing closed to wrongly launching"
MUTANT10="$WORK/bin/mutant-old-handoff-gate.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT10" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = 'if [ "$IS_SCHEDULED_RUN" -eq 1 ] && [ -n "${CUTOFF_EPOCH:-}" ]; then\n  if [ "$POST_COPY_NOW_EPOCH" -ge "$CUTOFF_EPOCH" ]; then\n'
assert needle in text, "mutation 10 target text not found; scheduled-review.sh changed shape"
old_gate = 'if [ -n "${CUTOFF_EPOCH:-}" ] && [ -n "${HANDOFF_EPOCH:-}" ] && [ "$POST_COPY_NOW_EPOCH" -lt "$HANDOFF_EPOCH" ]; then\n  if [ "$POST_COPY_NOW_EPOCH" -ge "$CUTOFF_EPOCH" ]; then\n'
mutated = text.replace(needle, old_gate, 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT10"

DATE_POST_HO_M="2026-11-13"
OUT_POST_HO_M="$ARCHIVE/$DATE_POST_HO_M"
CASKROOT_HOM="$WORK/opt-fake-hom"
CASK_PKG_HOM="$CASKROOT_HOM/Caskroom/codex/6.0.1"
mkdir -p "$CASK_PKG_HOM/bin"
cp "$BIN/codex" "$CASK_PKG_HOM/bin/codex"
chmod +x "$CASK_PKG_HOM/bin/codex"
mkdir -p "$CASKROOT_HOM/bin"
ln -sf "../Caskroom/codex/6.0.1/bin/codex" "$CASKROOT_HOM/bin/codex"
COPYROOT_HOM="$WORK/reviewer-copy-hom"
rm -rf "$COPYROOT_HOM"
write_control_for "$DATE_POST_HO_M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW_POST_HOM="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:00" +%s)"
POST_COPY_HOM="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 06:01" +%s)"
FAKE_NOW_EPOCH="$NOW_POST_HOM" FAKE_CODEX_BIN="$CASKROOT_HOM/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_HOM" \
  FAKE_POST_COPY_NOW_EPOCH="$POST_COPY_HOM" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_POST_HO_M" "$MUTANT10"
ok test "$RC" -eq 0
ok test -f "$OUT_POST_HO_M/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -ge 1

note "control: the SAME past-handoff scenario, against the REAL (unmutated) script, still fails closed"
DATE_POST_HO_C="2026-11-14"
OUT_POST_HO_C="$ARCHIVE/$DATE_POST_HO_C"
CASKROOT_HOC="$WORK/opt-fake-hoc"
CASK_PKG_HOC="$CASKROOT_HOC/Caskroom/codex/6.0.2"
mkdir -p "$CASK_PKG_HOC/bin"
cp "$BIN/codex" "$CASK_PKG_HOC/bin/codex"
chmod +x "$CASK_PKG_HOC/bin/codex"
mkdir -p "$CASKROOT_HOC/bin"
ln -sf "../Caskroom/codex/6.0.2/bin/codex" "$CASKROOT_HOC/bin/codex"
COPYROOT_HOC="$WORK/reviewer-copy-hoc"
rm -rf "$COPYROOT_HOC"
write_control_for "$DATE_POST_HO_C" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW_POST_HOC="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:00" +%s)"
POST_COPY_HOC="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 06:01" +%s)"
FAKE_NOW_EPOCH="$NOW_POST_HOC" FAKE_CODEX_BIN="$CASKROOT_HOC/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_HOC" \
  FAKE_POST_COPY_NOW_EPOCH="$POST_COPY_HOC" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_POST_HO_C"
ok test "$RC" -ne 0
ok test ! -f "$OUT_POST_HO_C/review-receipt.json"

note "mc-vg49y Part A (the cross-reviewer's cross-review of 502c0b60, finding 3): prune leaves an unmarked sibling directory alone"
COPYROOT_PRUNE="$WORK/reviewer-copy-prune"
rm -rf "$COPYROOT_PRUNE"
mkdir -p "$COPYROOT_PRUNE/5.5.5"
echo "not ours" > "$COPYROOT_PRUNE/5.5.5/some-file.txt"
CASKROOT5="$WORK/opt-fake5"
CASK_PKG5="$CASKROOT5/Caskroom/codex/8.8.8"
mkdir -p "$CASK_PKG5/bin"
cp "$BIN/codex" "$CASK_PKG5/bin/codex"
chmod +x "$CASK_PKG5/bin/codex"
mkdir -p "$CASKROOT5/bin"
ln -sf "../Caskroom/codex/8.8.8/bin/codex" "$CASKROOT5/bin/codex"

DATE_A6="2026-10-01"
OUT_A6="$ARCHIVE/$DATE_A6"
write_control_for "$DATE_A6" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_CODEX_BIN="$CASKROOT5/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_PRUNE" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A6"
ok test "$RC" -eq 0
ok test -f "$OUT_A6/review-receipt.json"
ok test -x "$COPYROOT_PRUNE/8.8.8/bin/codex"
ok test -f "$COPYROOT_PRUNE/8.8.8/.rmw-reviewer-copy"
# The unmarked sibling "5.5.5" (no marker file) is left completely alone -
# prune_reviewer_copies requires BOTH a version-looking name AND the marker
# before it will delete anything.
ok test -d "$COPYROOT_PRUNE/5.5.5"
ok test -f "$COPYROOT_PRUNE/5.5.5/some-file.txt"

note "mc-vg49y Part A (the cross-reviewer's cross-review of 502c0b60, finding 3): an existing final_dir that cannot be removed falls back to CODEX_BIN, not a silent wrong success"
COPYROOT_IMMUTABLE="$WORK/reviewer-copy-immutable"
rm -rf "$COPYROOT_IMMUTABLE"
mkdir -p "$COPYROOT_IMMUTABLE/3.3.3"
echo "pre-existing, not a valid copy" > "$COPYROOT_IMMUTABLE/3.3.3/sentinel.txt"
chflags uchg "$COPYROOT_IMMUTABLE/3.3.3"
CASKROOT6="$WORK/opt-fake6"
CASK_PKG6="$CASKROOT6/Caskroom/codex/3.3.3"
mkdir -p "$CASK_PKG6/bin"
cp "$BIN/codex" "$CASK_PKG6/bin/codex"
chmod +x "$CASK_PKG6/bin/codex"
mkdir -p "$CASKROOT6/bin"
ln -sf "../Caskroom/codex/3.3.3/bin/codex" "$CASKROOT6/bin/codex"

DATE_A7="2026-10-02"
OUT_A7="$ARCHIVE/$DATE_A7"
write_control_for "$DATE_A7" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_CODEX_BIN="$CASKROOT6/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_IMMUTABLE" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A7"
ok test "$RC" -eq 0
ok test -f "$OUT_A7/review-receipt.json"
ok grep -q "could not remove the existing $COPYROOT_IMMUTABLE/3.3.3 to install the verified copy; using $CASKROOT6/bin/codex directly" "$LOG_FILE"
# The pre-existing directory is untouched - never silently replaced, and
# nothing from the attempted copy landed inside it (which is what a bare
# `mv tmp_dir existing_dir` would have done: moved tmp_dir INSIDE it rather
# than failing).
ok test -f "$COPYROOT_IMMUTABLE/3.3.3/sentinel.txt"
ok bash -c "[ \"\$(find '$COPYROOT_IMMUTABLE/3.3.3' -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')\" = 1 ]"
chflags nouchg "$COPYROOT_IMMUTABLE/3.3.3"

note "mc-vg49y Part B: stuck-probe says stuck once -> exactly one relaunch, second attempt succeeds"
STUCK_PROBE_ONCE="$BIN/stuck-probe-once.sh"
cat > "$STUCK_PROBE_ONCE" <<'SH'
#!/usr/bin/env bash
echo "stuck-probe-called $*" >> "$CALL_LOG"
exit 0
SH
chmod +x "$STUCK_PROBE_ONCE"

DATE_B1="2026-09-26"
OUT_B1="$ARCHIVE/$DATE_B1"
write_control_for "$DATE_B1" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS_1 30
set_control_var FAKE_IGNORE_TERM_1 1
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_ONCE" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B1"
ok test "$RC" -eq 0
ok test -f "$OUT_B1/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 2
ok test "$(calls_matching 'stuck-probe-called ')" -eq 1
ok grep -q "looks stuck at launch after 1s" "$LOG_FILE"
ok grep -q "killed after 1s stuck at launch" "$LOG_FILE"
ok grep -q "reviewer attempt 2 launched" "$LOG_FILE"
ok test -f "$WORK/tmp/scheduled-review-reviewer-$DATE_B1-attempt1.log"

note "mc-vg49y Part B: stuck on both attempts -> no third launch; existing timeout path runs against the second attempt"
STUCK_PROBE_ALWAYS="$BIN/stuck-probe-always.sh"
cat > "$STUCK_PROBE_ALWAYS" <<'SH'
#!/usr/bin/env bash
echo "stuck-probe-called $*" >> "$CALL_LOG"
exit 0
SH
chmod +x "$STUCK_PROBE_ALWAYS"

DATE_B2="2026-09-27"
OUT_B2="$ARCHIVE/$DATE_B2"
write_control_for "$DATE_B2" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 30
set_control_var FAKE_IGNORE_TERM 1
# mc-vg49y (the cross-reviewer's cross-review of 502c0b60, finding 2): the budget must
# leave real room after the detect+kill-grace window for a relaunch to be
# considered worthwhile at all, or this scenario would fall into the
# "too close to the deadline" no-relaunch path instead - a DIFFERENT
# scenario (covered separately below). 10s total, 1s to detect, 1s grace:
# plenty of margin before the 10s deadline for the relaunch decision to
# say yes, then attempt 2 (same sleep/ignore-term) runs out the clock.
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_TIMEOUT_SECONDS=10 FAKE_KILL_GRACE_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_ALWAYS" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B2"
ok test "$RC" -ne 0
ok grep -q "timed out" "$LOG_FILE"
ok test ! -f "$OUT_B2/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 2
ok test "$(calls_matching 'stuck-probe-called ')" -eq 1
ok grep -q "killed after 1s stuck at launch" "$LOG_FILE"
ok grep -q "reviewer attempt 2 launched" "$LOG_FILE"

note "mc-vg49y Part B: stuck-probe says not-stuck -> no relaunch"
STUCK_PROBE_NEVER="$BIN/stuck-probe-never.sh"
cat > "$STUCK_PROBE_NEVER" <<'SH'
#!/usr/bin/env bash
echo "stuck-probe-called $*" >> "$CALL_LOG"
exit 1
SH
chmod +x "$STUCK_PROBE_NEVER"

DATE_B3="2026-09-28"
OUT_B3="$ARCHIVE/$DATE_B3"
write_control_for "$DATE_B3" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 2
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_NEVER" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B3"
ok test "$RC" -eq 0
ok test -f "$OUT_B3/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 1
ok test "$(calls_matching 'stuck-probe-called ')" -eq 1
ok bash -c "! grep -q 'reviewer attempt 2 launched' '$LOG_FILE'"

# mc-cldya: this process has started, so the old pre-main RSS/CPU probe
# returns not-stuck. Only the real startup-log predicate can recover it.
note "mc-cldya marker-only startup is relaunched once even when the old process probe says not-stuck"
DATE_C1="2027-01-11"
OUT_C1="$ARCHIVE/$DATE_C1"
write_control_for "$DATE_C1" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_STARTUP_OUTPUT_1 'Reading additional input from stdin...'
set_control_var FAKE_SLEEP_SECONDS_1 30
set_control_var FAKE_IGNORE_TERM_1 1
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_NEVER" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_C1"
ok test "$RC" -eq 0
ok test -f "$OUT_C1/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 2
ok cmp -s "$WORK/tmp/scheduled-review-reviewer-$DATE_C1-attempt1.log" <(printf '%s\n' 'Reading additional input from stdin...')

note "mc-cldya a real startup header prevents marker-only recovery"
DATE_C2="2027-01-12"
OUT_C2="$ARCHIVE/$DATE_C2"
write_control_for "$DATE_C2" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_STARTUP_OUTPUT $'Reading additional input from stdin...\nOpenAI Codex test header'
set_control_var FAKE_SLEEP_SECONDS 2
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_NEVER" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_C2"
ok test "$RC" -eq 0
ok test -f "$OUT_C2/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 1

note "mc-cldya marker-only on both attempts cannot gain a third launch or publish a receipt"
DATE_C3="2027-01-13"
OUT_C3="$ARCHIVE/$DATE_C3"
write_control_for "$DATE_C3" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_STARTUP_OUTPUT 'Reading additional input from stdin...'
set_control_var FAKE_SLEEP_SECONDS 30
set_control_var FAKE_IGNORE_TERM 1
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_TIMEOUT_SECONDS=10 FAKE_KILL_GRACE_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_NEVER" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_C3"
ok test "$RC" -ne 0
ok test ! -f "$OUT_C3/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 2
ok grep -q "timed out" "$LOG_FILE"

note "mc-vg49y Part B (the cross-reviewer's cross-review of 502c0b60, finding 2): stuck first launch close to the deadline -> no relaunch, existing timeout path runs"
STUCK_PROBE_ONCE_DL="$BIN/stuck-probe-once-dl.sh"
cat > "$STUCK_PROBE_ONCE_DL" <<'SH'
#!/usr/bin/env bash
echo "stuck-probe-called $*" >> "$CALL_LOG"
exit 0
SH
chmod +x "$STUCK_PROBE_ONCE_DL"

DATE_B4="2026-10-29"
OUT_B4="$ARCHIVE/$DATE_B4"
write_control_for "$DATE_B4" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS_1 30
set_control_var FAKE_IGNORE_TERM_1 1
# 2s total budget, detect at 1s, kill grace 2s: by the time attempt 1 is
# confirmed dead (worst case ~1s detect + 2s grace = 3s), the single shared
# deadline (2s from launch) is already behind - well under one kill-grace
# window - so this must NOT relaunch; it falls through to the ordinary
# deadline/timeout path exactly as if there had been no stuck-detection at
# all.
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_TIMEOUT_SECONDS=2 FAKE_KILL_GRACE_SECONDS=2 FAKE_STUCK_PROBE="$STUCK_PROBE_ONCE_DL" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B4"
ok test "$RC" -ne 0
ok grep -q "not relaunching" "$LOG_FILE"
ok grep -q "timed out" "$LOG_FILE"
ok test ! -f "$OUT_B4/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 1
ok test "$(calls_matching 'stuck-probe-called ')" -eq 1
ok bash -c "! grep -q 'reviewer attempt 2 launched' '$LOG_FILE'"

note "mc-vg49y Part B (the cross-reviewer's cross-review of 502c0b60, finding 2): the poll loop ends by the wall-clock deadline, not by counting iterations"
DATE_B5="2026-10-30"
OUT_B5="$ARCHIVE/$DATE_B5"
write_control_for "$DATE_B5" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 10
set_control_var FAKE_IGNORE_TERM 1
B5_START="$(date +%s)"
# LAUNCH_WATCH huge: the stuck-relaunch path never fires here on purpose -
# this test is only about the outer deadline being wall-clock-based.
FAKE_LAUNCH_WATCH_SECONDS=100000 FAKE_TIMEOUT_SECONDS=2 FAKE_KILL_GRACE_SECONDS=1 \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B5"
B5_END="$(date +%s)"
B5_ELAPSED=$((B5_END - B5_START))
ok test "$RC" -ne 0
ok grep -q "timed out" "$LOG_FILE"
ok test ! -f "$OUT_B5/review-receipt.json"
# Bounded by the 2s budget + 1s kill grace + ordinary process/test overhead -
# proves the loop is driven by the wall clock (date +%s each tick), not by
# an iteration counter that could drift past the nominal budget under load.
ok test "$B5_ELAPSED" -lt 12

if [ "$FAIL" -eq 0 ]; then
  echo
  echo "all scheduled-review checks passed"
else
  echo
  echo "$FAIL check(s) failed"
fi
BASE_FAIL="$FAIL"
FAIL=0

# =============================================================================
# MUTATION CHECKS. Each disables one fix in a disposable copy of the real
# script under $WORK (never the file on disk), re-runs the scenario that
# proves it, and confirms the scenario now WRONGLY succeeds - proving the
# assertion above is load-bearing. Nothing here ever edits scheduled-
# review.sh itself.
# =============================================================================
note "MUTATION 1: skipping BOTH check-against-real-files gates (pre-publish and the post-publish re-check) flips the no-verdict AND staged-tamper cases wrong"
# There are two independent check gates in the real script (pre-publish, and
# a re-check of the final published path) - defense in depth beyond what was
# asked for. Disabling only one leaves the other catching a bad receipt, so
# this mutation disables both at once to prove the check step as a whole is
# load-bearing, not that either half alone is redundant.
MUTANT1="$WORK/bin/mutant-skip-check.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT1" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
pre_needle = (
    'CHECK_LOG="$TMP/scheduled-review-check-$DATE.log"\n'
    'if ! "$PYTHON_BIN" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check \\\n'
    '    --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --receipt "$STAGE_RECEIPT" \\\n'
    '    --producer "$PRODUCER" >"$CHECK_LOG" 2>&1; then\n'
)
post_needle = (
    'if ! "$PYTHON_BIN" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check \\\n'
    '    --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \\\n'
    '    --producer "$PRODUCER" >>"$LOG_FILE" 2>&1; then\n'
)
assert pre_needle in text, "mutation 1 pre-publish target text not found; scheduled-review.sh changed shape"
assert post_needle in text, "mutation 1 post-publish target text not found; scheduled-review.sh changed shape"
mutated = text.replace(pre_needle, 'CHECK_LOG="$TMP/scheduled-review-check-$DATE.log"\nif false; then\n', 1)
mutated = mutated.replace(post_needle, 'if false; then\n', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT1"

DATE3M="2026-10-03"
OUT3M="$ARCHIVE/$DATE3M"
write_control_for "$DATE3M" "card-a:yes,yes,yes" "card-b:no,yes,yes:missing context for card-b"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE3M" "$MUTANT1"
ok test "$RC" -eq 0
ok test -f "$OUT3M/review-receipt.json"

DATE8M="2026-10-08"
OUT8M="$ARCHIVE/$DATE8M"
write_control_for "$DATE8M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_MUTATE_PATH "$(stage_pdf "$DATE8M")"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE8M" "$MUTANT1"
ok test "$RC" -eq 0
ok test -f "$OUT8M/review-receipt.json"

note "MUTATION 2: skipping the post-review real-file re-hash still refuses a field-preserving manifest tamper through the receipt's file hash"
MUTANT2="$WORK/bin/mutant-skip-rehash.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT2" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    'REAL_PDF_SHA_AFTER="$(sha256_of "$PDF")" || fail "could not hash the real PDF at $PDF after review"\n'
    'REAL_MANIFEST_SHA_AFTER="$(sha256_of "$MANIFEST")" || fail "could not hash the real manifest at $MANIFEST after review"\n'
    'if [ "$REAL_PDF_SHA_AFTER" != "$REAL_PDF_SHA_BEFORE" ]; then\n'
    '  fail "the real PDF at $PDF changed during the review window; refusing to publish"\n'
    'fi\n'
    'if [ "$REAL_MANIFEST_SHA_AFTER" != "$REAL_MANIFEST_SHA_BEFORE" ]; then\n'
    '  fail "the real manifest at $MANIFEST changed during the review window; refusing to publish"\n'
    'fi\n'
)
assert needle in text, "mutation 2 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(needle, ': # mutation 2: real-file re-hash disabled\n', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT2"

# The new receipt manifest_sha256 makes this a defense-in-depth check: even
# when the before/after re-hash is disabled, the receipt check must refuse
# the changed real manifest bytes.
DATE10M="2026-10-10"
OUT10M="$ARCHIVE/$DATE10M"
write_control_for "$DATE10M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_MUTATE_MANIFEST_FIELD "$OUT10M/decisions-$DATE10M.manifest.json"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE10M" "$MUTANT2"
ok test "$RC" -ne 0
ok grep -q "manifest has changed since it was reviewed" "$LOG_FILE"
ok test ! -f "$OUT10M/review-receipt.json"

note "control: the SAME field-preserving real-manifest tamper, against the REAL (unmutated) script, still fails"
DATE10C="2026-10-20"
OUT10C="$ARCHIVE/$DATE10C"
write_control_for "$DATE10C" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_MUTATE_MANIFEST_FIELD "$OUT10C/decisions-$DATE10C.manifest.json"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE10C"
ok test "$RC" -ne 0
ok grep -q "real manifest" "$LOG_FILE"
ok test ! -f "$OUT10C/review-receipt.json"

note "MUTATION 3: treating a nonzero reviewer exit as non-fatal flips the crashed-reviewer case wrong"
MUTANT3="$WORK/bin/mutant-ignore-reviewer-rc.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT3" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    'if [ "$REVIEWER_RC" -ne 0 ]; then\n'
    '  keep_reviewer_log\n'
    '  fail "independent reviewer (codex exec) exited $REVIEWER_RC; not trusting anything it wrote"\n'
    'fi\n'
)
assert needle in text, "mutation 3 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(needle, ': # mutation 3: nonzero reviewer exit no longer fatal\n', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT3"

DATE11M="2026-10-11"
OUT11M="$ARCHIVE/$DATE11M"
write_control_for "$DATE11M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_EXIT_CODE 1
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE11M" "$MUTANT3"
ok test "$RC" -eq 0
ok test -f "$OUT11M/review-receipt.json"

note "MUTATION 4: removing the timeout kill loop flips the runaway-reviewer case wrong (bounded: fake sleeps 5s and exits clean)"
MUTANT4="$WORK/bin/mutant-no-timeout.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT4" <<'PY'
import sys, re
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
start_marker = 'TIMED_OUT=0\nSTUCK_CHECKED=0\n'
end_marker = 'wait "$REVIEWER_PID" 2>/dev/null\nREVIEWER_RC=$?\n'
start = text.index(start_marker) + len(start_marker)
end = text.index(end_marker, start) + len(end_marker)
assert start > 0 and end > start, "mutation 4 markers not found; scheduled-review.sh changed shape"
replacement = 'TIMED_OUT=0\nwait "$REVIEWER_PID" 2>/dev/null\nREVIEWER_RC=$?\n'
mutated = text[:start] + replacement + text[end:]
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT4"

DATE15M="2026-10-15"
OUT15M="$ARCHIVE/$DATE15M"
write_control_for "$DATE15M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS 5
FAKE_TIMEOUT_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE15M" "$MUTANT4"
ok test "$RC" -eq 0
ok test -f "$OUT15M/review-receipt.json"

note "MUTATION 5: a lock takeover with no liveness check steals a live-owner's lock"
MUTANT5="$WORK/bin/mutant-lock-age-only.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT5" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    '  if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then\n'
    '    return 1\n'
    '  fi\n'
)
assert needle in text, "mutation 5 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(needle, '', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT5"

LIVE_LOCK5="$WORK/lock.dir"
rm -rf "$LIVE_LOCK5"
mkdir -p "$LIVE_LOCK5"
echo "$TEST_PID" > "$LIVE_LOCK5/pid"
DATE6M="2026-10-06"
write_control_for "$DATE6M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE6M" "$MUTANT5"
ok test "$RC" -eq 0
rm -rf "$LIVE_LOCK5"

note "MUTATION 6: removing the DATE regex guard lets a malformed date reach the pipeline"
MUTANT6="$WORK/bin/mutant-no-date-check.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT6" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    'case "$DATE" in\n'
    '  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;\n'
    '  *)\n'
    '    echo "Usage: $(basename "$0") [--date YYYY-MM-DD]" >&2\n'
    '    echo "  --date must match YYYY-MM-DD; got: $DATE" >&2\n'
    '    exit 2\n'
    '    ;;\n'
    'esac\n'
)
assert needle in text, "mutation 6 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(needle, '', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT6"

BAD1M="not-a-date"
write_control_for "$BAD1M"
run_scheduled_review "$BAD1M" "$MUTANT6"
ok test "$RC" -ne 2

note "MUTATION 7: removing the cutoff/handoff gate lets a too-late start proceed"
MUTANT7="$WORK/bin/mutant-no-cutoff.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT7" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    'if [ "$TOO_LATE_TO_START" -eq 1 ]; then\n'
    '  fail "started too late to finish before 06:00 (now $NOW_EPOCH, cutoff $CUTOFF_HHMM, handoff $HANDOFF_HHMM)"\n'
    'fi\n'
)
assert needle in text, "mutation 7 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(needle, '', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT7"

DATE13M="2026-10-13"
write_control_for "$DATE13M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
NOW13M="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:50" +%s)"
FAKE_NOW_EPOCH="$NOW13M" FAKE_CUTOFF="05:40" FAKE_HANDOFF="06:00" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE13M" "$MUTANT7"
# Without the start gate, a too-late run now spends verify and render time
# (the base case proves those are never called); the separate launch-time
# recheck still stops the reviewer, which is defense in depth, not a pass.
ok test "$(calls_matching 'verify-queue ')" -ge 1

note "MUTATION 8 (mc-vg49y): disabling the one allowed relaunch flips the stuck-once case from recovering to failing - proves the relaunch step itself is load-bearing"
MUTANT8="$WORK/bin/mutant-no-relaunch.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT8" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    '      wait "$REVIEWER_PID" 2>/dev/null\n'
    '      launch_reviewer 2\n'
)
assert needle in text, "mutation 8 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(
    needle,
    '      wait "$REVIEWER_PID" 2>/dev/null\n'
    '      : # mutation 8: relaunch disabled\n',
    1,
)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT8"

DATE_B1M="2026-10-26"
OUT_B1M="$ARCHIVE/$DATE_B1M"
write_control_for "$DATE_B1M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS_1 30
set_control_var FAKE_IGNORE_TERM_1 1
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_ONCE" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B1M" "$MUTANT8"
ok test "$RC" -ne 0
ok test ! -f "$OUT_B1M/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -eq 1

note "control: the SAME stuck-once scenario, against the REAL (unmutated) script, still recovers and passes"
DATE_B1C="2026-10-27"
OUT_B1C="$ARCHIVE/$DATE_B1C"
write_control_for "$DATE_B1C" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
set_control_var FAKE_SLEEP_SECONDS_1 30
set_control_var FAKE_IGNORE_TERM_1 1
FAKE_LAUNCH_WATCH_SECONDS=1 FAKE_KILL_GRACE_SECONDS=1 FAKE_STUCK_PROBE="$STUCK_PROBE_ONCE" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_B1C"
ok test "$RC" -eq 0
ok test -f "$OUT_B1C/review-receipt.json"

note "MUTATION 9 (mc-vg49y, the cross-reviewer's cross-review of 502c0b60, finding 1): disabling the post-copy cutoff recheck lets a slow-copy-crosses-cutoff run wrongly proceed to launch"
MUTANT9="$WORK/bin/mutant-no-post-copy-cutoff.sh"
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT9" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = (
    'if [ "$IS_SCHEDULED_RUN" -eq 1 ] && [ -n "${CUTOFF_EPOCH:-}" ]; then\n'
    '  if [ "$POST_COPY_NOW_EPOCH" -ge "$CUTOFF_EPOCH" ]; then\n'
    '    fail "resolving the reviewer binary finished after the $CUTOFF_HHMM cutoff; no time left to review before $HANDOFF_HHMM"\n'
    '  fi\n'
    '  REMAINING_AFTER_COPY=$(( CUTOFF_EPOCH - POST_COPY_NOW_EPOCH ))\n'
    '  if [ "$REMAINING_AFTER_COPY" -lt "$EFFECTIVE_TIMEOUT_SECONDS" ]; then\n'
    '    EFFECTIVE_TIMEOUT_SECONDS="$REMAINING_AFTER_COPY"\n'
    '  fi\n'
    'fi\n'
)
assert needle in text, "mutation 9 target text not found; scheduled-review.sh changed shape"
mutated = text.replace(needle, ': # mutation 9: post-copy cutoff recheck disabled entirely\n', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT9"

DATE_A5M="2026-11-05"
OUT_A5M="$ARCHIVE/$DATE_A5M"
CASKROOT4M="$WORK/opt-fake4m"
CASK_PKG4M="$CASKROOT4M/Caskroom/codex/4.0.1"
mkdir -p "$CASK_PKG4M/bin"
cp "$BIN/codex" "$CASK_PKG4M/bin/codex"
chmod +x "$CASK_PKG4M/bin/codex"
mkdir -p "$CASKROOT4M/bin"
ln -sf "../Caskroom/codex/4.0.1/bin/codex" "$CASKROOT4M/bin/codex"
COPYROOT_SLOW_M="$WORK/reviewer-copy-slow-mutant"
rm -rf "$COPYROOT_SLOW_M"
write_control_for "$DATE_A5M" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
TODAY="$(date +%F)"
CUTOFF_EPOCH_A5M="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY 05:40" +%s)"
POST_COPY_A5M=$(( CUTOFF_EPOCH_A5M + 60 ))
FAKE_CODEX_BIN="$CASKROOT4M/bin/codex" FAKE_REVIEWER_COPY_ROOT="$COPYROOT_SLOW_M" \
  FAKE_POST_COPY_NOW_EPOCH="$POST_COPY_A5M" \
  FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_scheduled_review "$DATE_A5M" "$MUTANT9"
ok test "$RC" -eq 0
ok test -f "$OUT_A5M/review-receipt.json"
ok test "$(calls_matching 'codex-called ')" -ge 1

echo
if [ "$FAIL" -eq 0 ]; then
  echo "all mutation and defense-in-depth checks passed"
else
  echo "$FAIL mutation check(s) failed"
fi

TOTAL_FAIL=$((BASE_FAIL + FAIL))
if [ "$TOTAL_FAIL" -eq 0 ]; then
  echo "all scheduled-review checks passed"
else
  echo "$TOTAL_FAIL check(s) failed total"
fi
exit "$TOTAL_FAIL"
