#!/usr/bin/env bash
# scheduled-review-preflight.sh, end to end, against fakes (mc-t5mb6).
#
# Same fake shape as test-scheduled-review.sh (the renderer and the
# independent reviewer are stubbed; nothing here reaches reportlab, a real
# codex session, or the device), because scheduled-review-preflight.sh does
# not reimplement scheduled-review.sh's logic - it wraps the real
# scheduled-review.sh through its own environment overrides, so the real
# script, and the real hash-bound review-receipt.py gate inside it, both run
# for real here.
#
# What this file proves that test-scheduled-review.sh does not:
#   - a "real" archive (standing in for thoughts/remarkable-archive) is
#     checksummed before and after a full run and comes out byte-identical,
#     whether the wrapped review passes or fails
#   - a mutation check that the untouched-archive assertion is load-bearing:
#     point the wrapper's scratch-archive variable at the "real" archive and
#     show the same assertion now fails
#   - a failing review's alert carries the "Decisions PRE-FLIGHT" subject
#     prefix and the "nothing was published" note
#   - a card-contract HELD row still alerts even when the review itself
#     passes clean
#   - a clean run (review passes, nothing held) sends no alert at all
#   - the preflight's own lock path never collides with the 05:00 job's lock
#   - fidelity (mc-t5mb6 cross-review, fix 1): with a fake "real archive"
#     holding a resolution recording one question already answered, a run
#     that seeds the scratch archive from it excludes that question exactly
#     as a normal render would - proven through the REAL verify-queue.py and
#     REAL push-decisions.sh/today_page.py, not the stock fakes, since no
#     fake exercises answer-record filtering
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/scheduled-review-preflight.sh"
RECEIPT_TOOL="$HERE/review-receipt.py"
PYTHON_BIN="$(command -v python3)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

REAL_ARCHIVE="$WORK/real-archive"     # stands in for thoughts/remarkable-archive
BIN="$WORK/bin"
FAKEHOME="$WORK/fakehome"
CALL_LOG="$WORK/calls.log"
KEEP_DIR="$WORK/keep"
FAIL=0

mkdir -p "$REAL_ARCHIVE" "$BIN" "$FAKEHOME" "$KEEP_DIR"

ok() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }
note() { echo; echo "--- $* ---"; }

# archive_checksum <dir> - one hash over every file's path and bytes, or the
# sentinel MISSING if the directory does not exist. Order-independent (sorted
# NUL-delimited paths) so a same-content-different-mtime write still counts
# as a change only when bytes or paths actually differ... no: a rewrite with
# the same bytes at the same path is legitimately "unchanged" for this
# purpose (the real-archive files are never touched at all, not just
# byte-stable), and shasum over content only (not mtime) is the right
# granularity - a script that touches a file's mtime without changing its
# content still touched it, but this test only needs to catch a WRITE, and a
# write always changes either the path set or the bytes.
archive_checksum() {
  local dir="$1"
  if [ ! -d "$dir" ]; then echo "MISSING"; return; fi
  ( cd "$dir" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256 2>/dev/null ) | shasum -a 256 | awk '{print $1}'
}

# --- fakes, mirrored from test-scheduled-review.sh -------------------------

cat > "$BIN/verify-queue.py" <<'PY'
import os, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
with open(os.environ["CALL_LOG"], "a") as f:
    f.write("verify-queue " + " ".join(sys.argv[1:]) + "\n")
open(args["--out-queue"], "w").write("| row | ask |\n")
open(args["--report"], "w").write('{"rows":[{"item_id":"row","verified":true,"held_reason":""}]}')
held = os.environ.get("FAKE_HELD_TITLE", "")
if held:
    print(f"  HELD: {held} -> {os.environ.get('FAKE_HELD_REASON', 'test reason')}", file=sys.stderr)
PY

cat > "$BIN/push-decisions.sh" <<'SH'
#!/usr/bin/env bash
echo "push-decisions $*" >> "$CALL_LOG"
date_arg=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --push) echo "push-decisions: --push must never be passed" >&2; exit 9 ;;
    --date) date_arg="$2"; shift 2 ;;
    *) shift ;;
  esac
done
rc="${FAKE_RENDER_RC:-0}"
[ "$rc" -eq 0 ] || exit "$rc"
# scheduled-review.sh passes REMARKABLE_TODAY_ARCHIVE (not ARCHIVE_ROOT) as
# an env var to the push-decisions.sh child (its own ARCHIVE_ROOT is a local
# bash variable derived from that, never exported by name) - this fake reads
# what actually arrives, not the real script's internal variable name.
outdir="$REMARKABLE_TODAY_ARCHIVE/$date_arg"
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

# The REAL bus-alert.sh underneath scheduled-review-preflight.sh's generated
# wrapper. Records every call with subject/body/slug on separate lines so a
# test can grep the prefix precisely.
cat > "$BIN/bus-alert.sh" <<'SH'
#!/usr/bin/env bash
{
  echo "bus-alert-called"
  echo "SUBJECT: $1"
  echo "BODY: $2"
  echo "SLUG: $3"
  echo "---"
} >> "$CALL_LOG"
exit 0
SH

cat > "$BIN/codex" <<'SH'
#!/usr/bin/env bash
# shellcheck disable=SC1090
. "$HOME/control.env"
echo "codex-called $*" >> "$CALL_LOG"
prev=""
cd_target=""
for a in "$@"; do
  if [ "$prev" = "-C" ]; then cd_target="$a"; fi
  prev="$a"
done
[ -n "$cd_target" ] && cd "$cd_target"
# The scratch STAGE_DIR is a fresh mktemp -d chosen by
# scheduled-review-preflight.sh at run time, so this fake cannot be handed
# its path in advance the way test-scheduled-review.sh's fixed $WORK/tmp
# lets it be. It does not need to be: scheduled-review.sh always names the
# staged files decisions-<date>.pdf / .manifest.json / review-receipt.json
# INSIDE STAGE_DIR (today's script, STAGE_PDF/STAGE_MANIFEST/STAGE_RECEIPT),
# and this fake has already cd'd there via -C above, so the same relative
# names resolve to the same files without knowing the scratch path.
verdict_args=()
i=1
while [ "$i" -le "${FAKE_VERDICT_COUNT:-0}" ]; do
  eval "v=\"\${FAKE_VERDICT_$i}\""
  verdict_args+=(--verdict "$v")
  i=$((i + 1))
done
"$FAKE_PYTHON" "$FAKE_RECEIPT_TOOL" write --date "$FAKE_DATE" \
  --reviewer scheduled-codex-reviewer \
  --pdf "decisions-$FAKE_DATE.pdf" \
  --manifest "decisions-$FAKE_DATE.manifest.json" \
  --receipt "review-receipt.json" \
  "${verdict_args[@]}"
exit "${FAKE_EXIT_CODE:-0}"
SH

cat > "$BIN/pdftoppm" <<'SH'
#!/usr/bin/env bash
prefix="${@: -1}"
printf 'png' > "${prefix}-1.png"
SH

chmod +x "$BIN/push-decisions.sh" "$BIN/bus-alert.sh" "$BIN/codex" "$BIN/pdftoppm"
: > "$BIN/prompt.md"

# write_control <date> [<verdict>...] - the fake codex resolves its own
# staged pdf/manifest/receipt paths by name after cd'ing into STAGE_DIR (see
# the fake above), so this only needs to hand it the date and the verdicts.
write_control() {
  local date="$1"
  shift
  {
    printf 'FAKE_PYTHON=%q\n' "$PYTHON_BIN"
    printf 'FAKE_RECEIPT_TOOL=%q\n' "$RECEIPT_TOOL"
    printf 'FAKE_DATE=%q\n' "$date"
    printf 'CALL_LOG=%q\n' "$CALL_LOG"
    printf 'FAKE_VERDICT_COUNT=%d\n' "$#"
    local i=1
    for v in "$@"; do
      printf 'FAKE_VERDICT_%d=%q\n' "$i" "$v"
      i=$((i + 1))
    done
  } > "$FAKEHOME/control.env"
}

calls_matching() { grep -c "^$1" "$CALL_LOG" 2>/dev/null || true; }

TEST_PID="$$"

# run_preflight <date> [<script>] - every scheduled-review.sh dependency
# EXCEPT the five scheduled-review-preflight.sh itself overrides
# (REMARKABLE_TODAY_ARCHIVE, SCHEDULED_REVIEW_LOCK, SCHEDULED_REVIEW_LOG,
# SCHEDULED_REVIEW_TMP, SCHEDULED_REVIEW_BUS_ALERT) is exported here and
# reaches the real scheduled-review.sh unchanged, through simple process
# inheritance - exactly proving that pass-through, not faking it.
#
# The five dependency fakes below (verify-queue, push-decisions, codex,
# pdftoppm, python) default to this file's stock fakes but honor an
# already-exported value from the caller first - the fidelity scenario
# further down needs the REAL verify-queue.py/push-decisions.sh (through a
# queue-fixture wrapper) to actually exercise today_page.py's answer-record
# filtering, which no fake can do.
run_preflight() {
  local date="$1"
  local script="${2:-$SCRIPT}"
  : > "$CALL_LOG"
  CALL_LOG="$CALL_LOG" \
  HOME="$FAKEHOME" \
  SCHEDULED_REVIEW_VERIFY_QUEUE="${SCHEDULED_REVIEW_VERIFY_QUEUE:-$BIN/verify-queue.py}" \
  SCHEDULED_REVIEW_PUSH_DECISIONS="${SCHEDULED_REVIEW_PUSH_DECISIONS:-$BIN/push-decisions.sh}" \
  SCHEDULED_REVIEW_RECEIPT_TOOL="$RECEIPT_TOOL" \
  SCHEDULED_REVIEW_CODEX="${SCHEDULED_REVIEW_CODEX:-$BIN/codex}" \
  SCHEDULED_REVIEW_PDFTOPPM="$BIN/pdftoppm" \
  SCHEDULED_REVIEW_PROMPT="$BIN/prompt.md" \
  SCHEDULED_REVIEW_PYTHON="$PYTHON_BIN" \
  SCHEDULED_REVIEW_TIMEOUT_SECONDS="${FAKE_TIMEOUT_SECONDS:-2400}" \
  SCHEDULED_REVIEW_KILL_GRACE_SECONDS="${FAKE_KILL_GRACE_SECONDS:-15}" \
  SCHEDULED_REVIEW_CUTOFF="${FAKE_CUTOFF:-05:40}" \
  SCHEDULED_REVIEW_HANDOFF="${FAKE_HANDOFF:-06:00}" \
  SCHEDULED_REVIEW_SHASUM="${FAKE_SHASUM:-shasum}" \
  SCHEDULED_REVIEW_PREFLIGHT_LOG="$WORK/preflight.log" \
  SCHEDULED_REVIEW_PREFLIGHT_LOCK="${FAKE_PREFLIGHT_LOCK:-$WORK/lock-preflight.dir}" \
  SCHEDULED_REVIEW_PREFLIGHT_KEEP_DIR="$KEEP_DIR" \
  SCHEDULED_REVIEW_PREFLIGHT_REAL_BUS_ALERT="$BIN/bus-alert.sh" \
  FAKE_RENDER_RC="${FAKE_RENDER_RC:-0}" \
  FAKE_CARDS="${FAKE_CARDS:-card-a card-b}" \
  FAKE_HELD_TITLE="${FAKE_HELD_TITLE:-}" \
  FAKE_HELD_REASON="${FAKE_HELD_REASON:-}" \
  MUTANT_REAL_ARCHIVE="${MUTANT_REAL_ARCHIVE:-}" \
    "$script" --date "$date" > "$WORK/out" 2> "$WORK/err"
  RC=$?
}

mkdir -p "$WORK/tmp"

# =============================================================================
note "clean pass: real archive untouched, no alert, exit 0"
DATE1="2030-09-24"
BEFORE1="$(archive_checksum "$REAL_ARCHIVE")"
write_control "$DATE1" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_preflight "$DATE1"
AFTER1="$(archive_checksum "$REAL_ARCHIVE")"
ok test "$RC" -eq 0
ok test "$BEFORE1" = "$AFTER1"
ok test "$(calls_matching 'bus-alert-called')" -eq 0
ok grep -q "clean - review passed" "$WORK/preflight.log"
ok test -f "$KEEP_DIR/scheduled-review-preflight-$DATE1.pdf"
ok grep -q "FAKE-PDF-BYTES-$DATE1" "$KEEP_DIR/scheduled-review-preflight-$DATE1.pdf"

# =============================================================================
note "failing review: real archive untouched, PRE-FLIGHT alert sent, exit 1"
DATE2="2030-09-25"
BEFORE2="$(archive_checksum "$REAL_ARCHIVE")"
write_control "$DATE2" "card-a:yes,yes,yes" "card-b:no,yes,yes:missing context for card-b"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_preflight "$DATE2"
AFTER2="$(archive_checksum "$REAL_ARCHIVE")"
ok test "$RC" -eq 1
ok test "$BEFORE2" = "$AFTER2"
ok test "$(calls_matching 'bus-alert-called')" -eq 1
ok grep -q "^SUBJECT: Decisions PRE-FLIGHT: Scheduled review FAILED for $DATE2" "$CALL_LOG"
ok grep -q "Nothing was published" "$CALL_LOG"
ok grep -q "05:00 scheduled-review run is unaffected" "$CALL_LOG"
ok grep -q "missing context for card-b" "$CALL_LOG"
ok grep -q "reviewer's receipt-check log kept" "$WORK/preflight.log"
ok test -f "$KEEP_DIR/scheduled-review-preflight-$DATE2-receipt-check.log"
ok grep -q "card-b" "$KEEP_DIR/scheduled-review-preflight-$DATE2-receipt-check.log"

# =============================================================================
note "HELD row present even though the review passes: alerted, exit 1"
DATE3="2030-09-26"
BEFORE3="$(archive_checksum "$REAL_ARCHIVE")"
write_control "$DATE3" "card-a:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a" \
  FAKE_HELD_TITLE="Held test row" FAKE_HELD_REASON="cites mc-fake which is closed" \
  run_preflight "$DATE3"
AFTER3="$(archive_checksum "$REAL_ARCHIVE")"
ok test "$RC" -eq 1
ok test "$BEFORE3" = "$AFTER3"
ok test "$(calls_matching 'bus-alert-called')" -eq 1
ok grep -q "^SUBJECT: Decisions PRE-FLIGHT: Held card(s) for $DATE3" "$CALL_LOG"
ok grep -q "Held test row" "$CALL_LOG"
ok grep -q "cites mc-fake which is closed" "$CALL_LOG"
ok grep -q "Nothing was published" "$CALL_LOG"

# =============================================================================
note "lock does not collide with the 05:00 job's lock"
# A "05:00 job" lock, held by a live process (this test's own pid stands in
# for a genuinely running holder - kill -0 on it succeeds, exactly like a
# real in-progress scheduled-review.sh run).
REAL_0500_LOCK="$WORK/lock-0500.dir"
mkdir -p "$REAL_0500_LOCK"
printf '%s\n' "$TEST_PID" > "$REAL_0500_LOCK/pid"
LOCK_0500_BEFORE="$(archive_checksum "$REAL_0500_LOCK")"
DATE4="2030-09-27"
write_control "$DATE4" "card-a:yes,yes,yes"
# The preflight's OWN lock path is deliberately different from the "05:00"
# lock above (run_preflight's own default, $WORK/lock-preflight.dir, is
# already distinct - this asserts that distinctness explicitly and proves
# the run is not blocked by the other lock being held).
FAKE_RENDER_RC=0 FAKE_CARDS="card-a" run_preflight "$DATE4"
LOCK_0500_AFTER="$(archive_checksum "$REAL_0500_LOCK")"
ok test "$RC" -eq 0
ok bash -c "[ '$WORK/lock-preflight.dir' != '$REAL_0500_LOCK' ]"
ok test ! -d "$WORK/lock-preflight.dir"    # scheduled-review.sh releases its own lock dir on exit
ok test "$LOCK_0500_BEFORE" = "$LOCK_0500_AFTER"    # the "05:00" lock was never touched or removed

# =============================================================================
note "usage: a malformed --date is exit 2, nothing touched"
BEFORE5="$(archive_checksum "$REAL_ARCHIVE")"
run_preflight "not-a-date"
AFTER5="$(archive_checksum "$REAL_ARCHIVE")"
ok test "$RC" -eq 2
ok test "$BEFORE5" = "$AFTER5"
ok test "$(calls_matching 'bus-alert-called')" -eq 0
ok test "$(calls_matching 'verify-queue ')" -eq 0

# =============================================================================
# mc-0kpzk: the reviewer log is kept outside this script's own scratch dir
# (which is removed unconditionally on exit) on a reviewer-related failure,
# mode 0600, path printed in the log; reviewer start/finish/elapsed are
# logged by the wrapped scheduled-review.sh on every run and survive the
# RUN_LOG-to-LOG_FILE merge into this script's own durable log. Dates chosen
# well outside any date this repo's real thoughts/remarkable-archive/
# today-pages could hold, so the fidelity seed never pulls in real (and
# therefore read-only, mode-444) manifests that would make the FAKE
# push-decisions.sh fail to write for reasons unrelated to this feature.
file_mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1" 2>/dev/null; }

note "mc-0kpzk: a passing run logs the timing line but keeps no reviewer log"
DATE_KEEP_PASS="2030-02-01"
KEPT_REVIEWER_PASS="$KEEP_DIR/scheduled-review-preflight-$DATE_KEEP_PASS-reviewer.log"
write_control "$DATE_KEEP_PASS" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_preflight "$DATE_KEEP_PASS"
ok test "$RC" -eq 0
ok grep -Eq "reviewer started .* finished .* elapsed [0-9]+s \(budget " "$WORK/preflight.log"
ok test ! -f "$KEPT_REVIEWER_PASS"

note "mc-0kpzk: a bad-receipt failure keeps the reviewer log (0600) outside the scratch dir"
DATE_KEEP_FAIL="2030-02-02"
KEPT_REVIEWER_FAIL="$KEEP_DIR/scheduled-review-preflight-$DATE_KEEP_FAIL-reviewer.log"
write_control "$DATE_KEEP_FAIL" "card-a:yes,yes,yes" "card-b:no,yes,yes:missing context for card-b"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_preflight "$DATE_KEEP_FAIL"
ok test "$RC" -eq 1
ok test -f "$KEPT_REVIEWER_FAIL"
ok test "$(file_mode "$KEPT_REVIEWER_FAIL")" = "600"
ok grep -q "reviewer log kept at $KEPT_REVIEWER_FAIL" "$WORK/preflight.log"
ok grep -Eq "reviewer started .* finished .* elapsed [0-9]+s \(budget " "$WORK/preflight.log"

note "mc-0kpzk: a fail-then-pass rerun on the SAME date does not report the earlier run's now-stale kept log as this run's own (the cross-reviewer's review of bd3f1b21)"
DATE_KEEP_RERUN="2030-02-03"
KEPT_REVIEWER_RERUN="$KEEP_DIR/scheduled-review-preflight-$DATE_KEEP_RERUN-reviewer.log"
write_control "$DATE_KEEP_RERUN" "card-a:yes,yes,yes" "card-b:no,yes,yes:missing context for card-b"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_preflight "$DATE_KEEP_RERUN"
ok test "$RC" -eq 1
ok test -f "$KEPT_REVIEWER_RERUN"
ok grep -q "reviewer log kept at $KEPT_REVIEWER_RERUN" "$WORK/preflight.log"
# preflight.log is cumulative (never truncated between runs, matching the
# real script's own durable log), so isolating THIS SECOND run's own
# contribution means only looking at what it appended, not the whole file.
LINES_BEFORE_RERUN="$(wc -l < "$WORK/preflight.log" | tr -d ' ')"
write_control "$DATE_KEEP_RERUN" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" run_preflight "$DATE_KEEP_RERUN"
ok test "$RC" -eq 0
# The earlier failing run's kept file is still sitting there, untouched by
# this clean rerun (only a failing run calls keep_reviewer_log) - the bug
# this proves fixed: the summary must not misreport this clean run as
# having just kept a reviewer log.
ok test -f "$KEPT_REVIEWER_RERUN"
ok bash -c "! tail -n +$((LINES_BEFORE_RERUN + 1)) '$WORK/preflight.log' | grep -q 'reviewer log kept at'"
ok bash -c "tail -n +$((LINES_BEFORE_RERUN + 1)) '$WORK/preflight.log' | grep -q 'clean - review passed'"

# =============================================================================
note "combined case: a failing review AND a HELD row in the same run produce ONE alert naming both (the cross-reviewer's review, blocker 1)"
DATE_COMBINED="2030-09-29"
BEFORE_COMBINED="$(archive_checksum "$REAL_ARCHIVE")"
write_control "$DATE_COMBINED" "card-a:yes,yes,yes" "card-b:no,yes,yes:missing context for card-b"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" \
  FAKE_HELD_TITLE="Combined-case held row" FAKE_HELD_REASON="cites mc-combined which is closed" \
  run_preflight "$DATE_COMBINED"
AFTER_COMBINED="$(archive_checksum "$REAL_ARCHIVE")"
ok test "$RC" -eq 1
ok test "$BEFORE_COMBINED" = "$AFTER_COMBINED"
# Exactly ONE bus-alert call for this run - not two (one for the failure,
# a second for the HELD row) - and that one call carries both.
ok test "$(calls_matching 'bus-alert-called')" -eq 1
ok grep -q "^SUBJECT: Decisions PRE-FLIGHT: Scheduled review FAILED for $DATE_COMBINED" "$CALL_LOG"
ok grep -q "missing context for card-b" "$CALL_LOG"
ok grep -q "Combined-case held row" "$CALL_LOG"
ok grep -q "cites mc-combined which is closed" "$CALL_LOG"
COMBINED_ALERT_INCLUDES_HELD="$(grep -q "Combined-case held row" "$CALL_LOG" && echo yes || echo no)"
echo "combined_alert_includes_held=$COMBINED_ALERT_INCLUDES_HELD"

# =============================================================================
note "unreadable seed source: an existing manifest that cannot be read fails the run and alerts (the cross-reviewer's review, blocker 2)"
UNREADABLE_MC="$WORK/unreadable-mc"
mkdir -p "$UNREADABLE_MC/remarkable"
cp "$SCRIPT" "$UNREADABLE_MC/remarkable/scheduled-review-preflight.sh"
cp "$HERE/scheduled-review.sh" "$UNREADABLE_MC/remarkable/scheduled-review.sh"
chmod +x "$UNREADABLE_MC/remarkable/"*.sh
UNREADABLE_DAY="$UNREADABLE_MC/thoughts/remarkable-archive/today-pages/2026-08-17"
mkdir -p "$UNREADABLE_DAY"
printf '{"cards":[]}\n' > "$UNREADABLE_DAY/decisions-2026-08-17.manifest.json"
chmod 000 "$UNREADABLE_DAY/decisions-2026-08-17.manifest.json"
DATE_UNREADABLE="2026-09-30"
write_control "$DATE_UNREADABLE" "card-a:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a" \
  RMW_ARCHIVE="$UNREADABLE_MC/thoughts/remarkable-archive" \
  RMW_DECISIONS_ARCHIVE="$UNREADABLE_MC/thoughts/remarkable-archive/today-pages" \
  run_preflight "$DATE_UNREADABLE" "$UNREADABLE_MC/remarkable/scheduled-review-preflight.sh"
UNREADABLE_RC=$RC
chmod 644 "$UNREADABLE_DAY/decisions-2026-08-17.manifest.json" 2>/dev/null || true
ok test "$UNREADABLE_RC" -eq 1
ok test "$(calls_matching 'bus-alert-called')" -eq 1
ok grep -q "^SUBJECT: Decisions PRE-FLIGHT: Fidelity seed FAILED for $DATE_UNREADABLE" "$CALL_LOG"
# The run must have stopped at the seed, never reaching the render (a
# silent-skip regression would let verify-queue run and report "clean").
ok test "$(calls_matching 'verify-queue ')" -eq 0

# =============================================================================
note "mktemp failure: an unwritable TMPDIR alerts, not just logs (the cross-reviewer's review, blocker 2)"
UNWRITABLE_TMP="$WORK/unwritable-tmp"
mkdir -p "$UNWRITABLE_TMP"
chmod 000 "$UNWRITABLE_TMP"
DATE_MKTEMP="2026-10-01"
write_control "$DATE_MKTEMP" "card-a:yes,yes,yes"
TMPDIR="$UNWRITABLE_TMP" FAKE_RENDER_RC=0 FAKE_CARDS="card-a" run_preflight "$DATE_MKTEMP"
MKTEMP_RC=$RC
chmod 755 "$UNWRITABLE_TMP" 2>/dev/null || true
ok test "$MKTEMP_RC" -eq 1
ok test "$(calls_matching 'bus-alert-called')" -eq 1
ok grep -q "^SUBJECT: Decisions PRE-FLIGHT: scratch directory FAILED for $DATE_MKTEMP" "$CALL_LOG"

# =============================================================================
note "fidelity: an already-answered row is excluded just as a normal render would (mc-t5mb6 fix 1)"
# The real verify-queue.py/push-decisions.sh/today_page.py, not the stock
# fakes: no fake touches ANSWER_RECORD_ROOT or filter_already_answered.
#
# scheduled-review-preflight.sh reads its seed source from
# $RMW_DECISIONS_ARCHIVE, where $MC is ITS OWN
# BASH_SOURCE-derived directory (two levels up) - not overridable by env var,
# exactly like SCHEDULED_REVIEW above. Proving the seed comes from the "real"
# archive and not the actual worktree archive means running a copy of the
# preflight script (and its sibling scheduled-review.sh, which it locates the
# same DIR-relative way) from inside a throwaway fake repo root whose
# thoughts/remarkable-archive IS the fixture.
FIDELITY_MC="$WORK/fidelity-mc"
mkdir -p "$FIDELITY_MC/remarkable"
cp "$SCRIPT" "$FIDELITY_MC/remarkable/scheduled-review-preflight.sh"
cp "$HERE/scheduled-review.sh" "$FIDELITY_MC/remarkable/scheduled-review.sh"
chmod +x "$FIDELITY_MC/remarkable/scheduled-review-preflight.sh" \
         "$FIDELITY_MC/remarkable/scheduled-review.sh"

# ANSWERED_QUESTION/UNANSWERED_QUESTION and this exact resolution.json shape
# mirror tests/test_remarkable_today_page.py's AlreadyAnsweredGuardTests
# fixture (ANSWERED_QUESTION, .archive()) - known-good against the real
# today_page.py, not invented here.
ANSWERED_QUESTION="Clear the old test copies?"
UNANSWERED_QUESTION="Approve the never-before-asked sample?"
FIDELITY_PROCESSED="$FIDELITY_MC/thoughts/remarkable-archive/today-pages/2026-08-16/processed"
mkdir -p "$FIDELITY_PROCESSED"
cat > "$FIDELITY_PROCESSED/resolution.json" <<JSON
{"page_id": "decisions-2026-08-16-fixture", "decisions": [{"entry_id": "decision-001a", "choice": "YES", "label": "$ANSWERED_QUESTION"}]}
JSON

DATE_FIDELITY="2026-08-20"
FIXTURE_QUEUE="$WORK/fixture-REVIEW-QUEUE.md"
cat > "$FIXTURE_QUEUE" <<QUEUE
# Queue

## Active - First

| Item | Project | Age | Ask |
|---|---|---|---|
| Choose this 0 | example-project | $DATE_FIDELITY | It is ready. It needs an answer. Recommendation: approve it. Decide: (1:q1) [yes/no] $ANSWERED_QUESTION Row: row_0 Read: tests/fixtures/remarkable/reading-sample.md |
| Choose this 1 | example-project | $DATE_FIDELITY | It is ready. It needs an answer. Recommendation: approve it. Decide: (1:q1) [yes/no] $UNANSWERED_QUESTION Row: row_1 Read: tests/fixtures/remarkable/reading-sample.md |
QUEUE

# A wrapper, not a copy: the REAL verify-queue.py must run from ITS OWN
# location (remarkable/verify-queue.py) so its sibling imports of
# today_page/question_state resolve, and so its `bd export` runs with the
# real repo as cwd. runpy prepends --queue pointing at the fixture above and
# executes the real module's own main() unmodified.
VERIFY_WRAPPER="$WORK/bin/verify-queue-with-fixture.py"
mkdir -p "$WORK/bin"
cat > "$VERIFY_WRAPPER" <<PY
import runpy, sys
sys.argv[1:1] = ["--queue", "$FIXTURE_QUEUE"]
runpy.run_path("$HERE/verify-queue.py", run_name="__main__")
PY

# A dynamic-verdict reviewer fake: unlike the stock $BIN/codex (fixed
# verdicts from control.env), this one reads whatever cards the REAL
# render actually produced and passes every one of them, since which
# row(s) survive filtering is exactly what this scenario is proving.
CODEX_AUTO="$WORK/bin/codex-auto"
cat > "$CODEX_AUTO" <<'SH'
#!/usr/bin/env bash
. "$HOME/control.env"
echo "codex-called $*" >> "$CALL_LOG"
prev=""
cd_target=""
for a in "$@"; do
  if [ "$prev" = "-C" ]; then cd_target="$a"; fi
  prev="$a"
done
[ -n "$cd_target" ] && cd "$cd_target"
manifest="decisions-$FAKE_DATE.manifest.json"
verdict_args=()
while IFS= read -r cid; do
  [ -n "$cid" ] && verdict_args+=(--verdict "$cid:yes,yes,yes")
done < <("$FAKE_PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); [print(c["card_id"]) for c in d.get("cards", [])]' "$manifest")
"$FAKE_PYTHON" "$FAKE_RECEIPT_TOOL" write --date "$FAKE_DATE" \
  --reviewer scheduled-codex-reviewer \
  --pdf "decisions-$FAKE_DATE.pdf" \
  --manifest "$manifest" \
  --receipt "review-receipt.json" \
  "${verdict_args[@]}"
exit 0
SH
chmod +x "$CODEX_AUTO"

write_control "$DATE_FIDELITY"
RMW_ARCHIVE="$FIDELITY_MC/thoughts/remarkable-archive" \
RMW_DECISIONS_ARCHIVE="$FIDELITY_MC/thoughts/remarkable-archive/today-pages" \
TODAY_EXTRACTOR_PYTHON="${RMW_RENDER_PYTHON:-python3}" \
SCHEDULED_REVIEW_VERIFY_QUEUE="$VERIFY_WRAPPER" \
SCHEDULED_REVIEW_PUSH_DECISIONS="$HERE/push-decisions.sh" \
SCHEDULED_REVIEW_CODEX="$CODEX_AUTO" \
  run_preflight "$DATE_FIDELITY" "$FIDELITY_MC/remarkable/scheduled-review-preflight.sh"

FIDELITY_MANIFEST="$KEEP_DIR/scheduled-review-preflight-$DATE_FIDELITY.manifest.json"
ok test "$RC" -eq 0
ok test -f "$FIDELITY_MANIFEST"
if [ -f "$FIDELITY_MANIFEST" ]; then
  # The excluded_already_answered list is SUPPOSED to name the excluded
  # question (that is how a person confirms why it is missing), so the
  # positive check belongs on the printed "cards" array specifically, not
  # on the whole manifest file - a substring check across the whole file
  # would wrongly fail on that legitimate record.
  ok "$PYTHON_BIN" -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
cards_text = json.dumps(manifest.get("cards", []))
assert "old test copies" not in cards_text, "the already-answered row printed a card"
assert "never-before-asked" in cards_text, "the still-open row did not print a card"
excluded = manifest.get("excluded_already_answered", [])
assert any("old test copies" in e.get("question", "") for e in excluded), \
    "the excluded_already_answered record does not name the answered question"
' "$FIDELITY_MANIFEST"
  CARD_COUNT="$("$PYTHON_BIN" -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("cards", [])))' "$FIDELITY_MANIFEST")"
  ok test "$CARD_COUNT" -eq 1
else
  echo "[FAIL] no manifest kept; cannot check card content (see $WORK/preflight.log)"
  FAIL=$((FAIL + 3))
fi

BASE_FAIL=$FAIL

# =============================================================================
note "mutation: point the scratch-archive variable at the real archive; the untouched assertion must now fail"
# The mutant has to live NEXT TO the real scheduled-review.sh: the preflight
# script locates it via its own BASH_SOURCE-derived directory
# (DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; SCHEDULED_REVIEW="$DIR/scheduled-review.sh"),
# exactly like scheduled-review.sh locates its own siblings - a mutant copy
# placed under $WORK would resolve that lookup to $WORK and fail to find it.
MUTANT="$HERE/.mutant-scheduled-review-preflight.$$.sh"
trap 'rm -rf "$WORK"; rm -f "$MUTANT"' EXIT
"$PYTHON_BIN" - "$SCRIPT" "$MUTANT" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = 'SCRATCH_ARCHIVE="$SCRATCH_DIR/today-pages"\n'
assert needle in text, "mutation target text not found; scheduled-review-preflight.sh changed shape"
mutated = text.replace(needle, 'SCRATCH_ARCHIVE="${MUTANT_REAL_ARCHIVE:?}"\n', 1)
assert mutated != text
open(dst, "w").write(mutated)
PY
chmod +x "$MUTANT"

DATE6="2030-09-28"
BEFORE6="$(archive_checksum "$REAL_ARCHIVE")"
write_control "$DATE6" "card-a:yes,yes,yes" "card-b:yes,yes,yes"
FAKE_RENDER_RC=0 FAKE_CARDS="card-a card-b" MUTANT_REAL_ARCHIVE="$REAL_ARCHIVE" \
  run_preflight "$DATE6" "$MUTANT"
AFTER6="$(archive_checksum "$REAL_ARCHIVE")"
MUTFAIL=0
if [ "$RC" -eq 0 ] && [ "$BEFORE6" != "$AFTER6" ]; then
  echo "[ok] mutation caught: the mutant's real-archive write changed the checksum, proving the assertion is load-bearing"
else
  echo "[FAIL] mutation not caught: mutant run rc=$RC, checksum before=$BEFORE6 after=$AFTER6"
  MUTFAIL=1
fi
FAIL=$((FAIL + MUTFAIL))
# Clean up what the mutant actually wrote into the real-archive stand-in so
# this test file leaves nothing behind for a re-run to trip over.
rm -rf "${REAL_ARCHIVE:?}/$DATE6" 2>/dev/null || true

echo
TOTAL_FAIL=$FAIL
if [ "$TOTAL_FAIL" -eq 0 ]; then
  echo "all scheduled-review-preflight checks passed"
else
  echo "$TOTAL_FAIL check(s) failed total"
fi
exit "$TOTAL_FAIL"
