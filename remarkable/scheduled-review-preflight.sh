#!/usr/bin/env bash
# scheduled-review-preflight.sh - an early (03:30) dry run of the 05:00
# scheduled-review.sh job (mc-t5mb6).
#
# The problem: on 2026-09-23 the 05:00 run correctly failed closed on two
# unclear cards, and getting a passing document took five manual re-runs
# between 05:01 and 05:09 (card rewrites, card-contract holds, reviewer
# variance). scheduled-review.sh has one attempt and fixing cards is author
# work; nothing ran that work early enough to matter.
#
# What this script does: renders today's Decisions document and has it
# independently reviewed EXACTLY as the 05:00 job would - same render chain,
# same isolated reviewer, same hash-bound gate - but PUBLISHES NOTHING and
# touches NOTHING in the real archive (thoughts/remarkable-archive) or the
# device. It does this by wrapping scheduled-review.sh, unmodified, through
# its own environment overrides: every path scheduled-review.sh and its
# render chain would write to is redirected into a throwaway scratch archive
# (a fresh mktemp -d), removed at the end. Nothing here reimplements
# scheduled-review.sh's render/review/gate logic.
#
# On completion, it tells the orchestrator on the bus which cards would fail review
# and which rows the card contract would hold, so the author can fix them
# before 05:00. A held row silently disappears from the document even when
# the review itself passes, so held rows are alerted regardless of the
# review's outcome; a clean run (review passed, nothing held) sends no
# alert, only a log line. Exactly ONE alert goes out per run (a review
# failure and HELD rows found in the same run are combined into one message,
# not two - a second cross-review pass found the failure path dropping HELD
# rows when both were true in the same run).
#
# Fail-closed fidelity: the scratch archive is seeded, before rendering,
# with read-only copies of the real archive's answer/question history (see
# "Fidelity seed" below). A missing real archive is an explicit, logged
# allowed case (a fresh checkout). An EXISTING source file that cannot be
# listed, read or copied stops the run and alerts - it never seeds a
# partial history and reports clean against a different card set than the
# 05:00 job would actually render.
#
# Usage:
#   scheduled-review-preflight.sh [--date YYYY-MM-DD]
#
# Exit codes:
#   0  clean: the review passed and the card contract held nothing
#   1  findings: the review failed, the card contract held row(s), or the
#      fidelity seed or scratch directory itself could not be built (every
#      case alerts - see the log for details)
#   2  usage (including a malformed --date); nothing is touched or removed
#
# Overridable for hermetic testing (remarkable/test-scheduled-review-preflight.sh):
#   SCHEDULED_REVIEW_PREFLIGHT_LOG            default /tmp/scheduled-review-preflight.log
#   SCHEDULED_REVIEW_PREFLIGHT_LOCK           default /tmp/scheduled-review-preflight.lock
#     (fed to scheduled-review.sh as ITS OWN SCHEDULED_REVIEW_LOCK - a path
#     distinct from the 05:00 job's default /tmp/scheduled-review.lock, so
#     this run can never block, or be blocked by, the real 05:00 job)
#   SCHEDULED_REVIEW_PREFLIGHT_KEEP_DIR       default /tmp
#     (where the rendered PDF, the reviewer's failed-receipt check log, and
#     (mc-0kpzk) the reviewer's raw transcript on a reviewer-related failure
#     are kept after the scratch archive is removed)
#   SCHEDULED_REVIEW_PREFLIGHT_REAL_BUS_ALERT default $DIR/hooks/alert.sh
#     (what the generated bus-alert wrapper forwards to)
#
# Every OTHER scheduled-review.sh override (SCHEDULED_REVIEW_VERIFY_QUEUE,
# SCHEDULED_REVIEW_PUSH_DECISIONS, SCHEDULED_REVIEW_CODEX,
# SCHEDULED_REVIEW_PDFTOPPM, SCHEDULED_REVIEW_RECEIPT_TOOL,
# SCHEDULED_REVIEW_PROMPT, SCHEDULED_REVIEW_PYTHON, SCHEDULED_REVIEW_CUTOFF,
# SCHEDULED_REVIEW_HANDOFF, SCHEDULED_REVIEW_TIMEOUT_SECONDS, ...) passes
# straight through unchanged: this script only sets the six listed in the
# design (REMARKABLE_TODAY_ARCHIVE, SCHEDULED_REVIEW_LOCK,
# SCHEDULED_REVIEW_LOG, SCHEDULED_REVIEW_TMP, SCHEDULED_REVIEW_BUS_ALERT,
# SCHEDULED_REVIEW_REVIEWER_KEEP_PATH - mc-0kpzk, added so a kept reviewer
# transcript lands in KEEP_DIR rather than the scratch dir this script
# removes on exit) and inherits everything else from the calling
# environment, exactly like any other command it runs.

set -uo pipefail

DIR_RMW="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Load the config into RMW_* variables. A copy of this script outside the
# repository (a test harness) falls back to RMW_PROJECT_ROOT, then to the
# built-in defaults below.
if [ -f "$DIR_RMW/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$DIR_RMW/rmw-env.sh"
elif [ -n "${RMW_PROJECT_ROOT:-}" ] && [ -f "$RMW_PROJECT_ROOT/remarkable/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$RMW_PROJECT_ROOT/remarkable/rmw-env.sh"
fi
# The same defaults rmw_config.py uses, for a run with no config loaded.
: "${RMW_PATHS_HOME:=$HOME/.local/share/remarkable-workflow}"
: "${RMW_ARCHIVE:=$RMW_PATHS_HOME/archive}"
: "${RMW_DECISIONS_ARCHIVE:=$RMW_ARCHIVE/decisions}"
: "${RMW_PATHS_QUEUE:=$RMW_PATHS_HOME/decisions-queue.md}"
: "${RMW_PATHS_REGISTRY:=$HOME/.remarkable-today-pages.json}"
: "${RMW_RENDER_PYTHON:=python3}"
: "${RMW_WRITER_IDENTITY:=writer}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MC="$(cd "$DIR/../.." && pwd)"

DATE="$(date +%F)"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --date) DATE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# DATE must be validated before ANY path is built or anything is touched -
# same discipline as scheduled-review.sh itself (mc-mi8xx hardening B6).
case "$DATE" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *)
    echo "Usage: $(basename "$0") [--date YYYY-MM-DD]" >&2
    echo "  --date must match YYYY-MM-DD; got: $DATE" >&2
    exit 2
    ;;
esac

SCHEDULED_REVIEW="$DIR/scheduled-review.sh"
REAL_BUS_ALERT="${SCHEDULED_REVIEW_PREFLIGHT_REAL_BUS_ALERT:-$DIR/hooks/alert.sh}"
LOG_FILE="${SCHEDULED_REVIEW_PREFLIGHT_LOG:-/tmp/scheduled-review-preflight.log}"
INNER_LOCK="${SCHEDULED_REVIEW_PREFLIGHT_LOCK:-/tmp/scheduled-review-preflight.lock}"
KEEP_DIR="${SCHEDULED_REVIEW_PREFLIGHT_KEEP_DIR:-/tmp}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
mkdir -p "$KEEP_DIR" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }
log "=== scheduled-review-preflight run for $DATE ==="

# ---------------------------------------------------------------------------
# Scratch archive. Every write scheduled-review.sh's render/review chain
# makes lands here - never in thoughts/remarkable-archive, never on the
# device. Removed unconditionally at the end (trap below); the rendered PDF
# and the reviewer's failed-receipt check log are copied out to KEEP_DIR
# first, further down, before that trap fires.
# ---------------------------------------------------------------------------
SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scheduled-review-preflight-XXXXXX" 2>/dev/null || true)"
if [ -z "$SCRATCH_DIR" ] || [ ! -d "$SCRATCH_DIR" ]; then
  log "$DATE: could not create a scratch directory; nothing done"
  # No scratch dir exists yet to build the usual bus-alert wrapper in, so
  # this one call goes straight to the real bus-alert.sh with the same
  # subject-prefix/note convention applied inline (the cross-reviewer's review,
  # blocker 2: this failure must alert, not just log).
  if "$REAL_BUS_ALERT" "Decisions PRE-FLIGHT: scratch directory FAILED for $DATE" \
      "scheduled-review-preflight.sh could not create a scratch working directory (mktemp -d under ${TMPDIR:-/tmp}), so no render or review was attempted for $DATE.

Nothing was published: the real Decisions archive and the reMarkable device were not touched. This is the $DATE pre-flight only; the 05:00 scheduled-review run is unaffected and still runs independently." \
      "preflight-scratch-failed-$DATE" >>"$LOG_FILE" 2>&1; then
    log "$DATE: scratch-directory-failure alert sent to the orchestrator"
  else
    log "$DATE: scratch-directory-failure alert failed to send; see log above"
  fi
  exit 1
fi
cleanup() { rm -rf "$SCRATCH_DIR" 2>/dev/null || true; }
trap cleanup EXIT

SCRATCH_ARCHIVE="$SCRATCH_DIR/today-pages"
SCRATCH_TMP="$SCRATCH_DIR/tmp"
mkdir -p "$SCRATCH_ARCHIVE" "$SCRATCH_TMP"

# ---------------------------------------------------------------------------
# Bus-alert wrapper. scheduled-review.sh's own fail() path would normally
# send an alert on every failure it detects; that call is INTERCEPTED here
# (pointed at a capture stub instead, further down) so this script can
# compose exactly ONE combined alert that also carries any card-contract
# HELD rows (the cross-reviewer's review, blocker 1: two separate alerts meant a
# failure's own alert never mentioned HELD rows found in the same run). This
# wrapper is what actually reaches the orchestrator - it prefixes the subject with
# "Decisions PRE-FLIGHT" and appends a note that nothing was published and
# the 05:00 run is unaffected, then forwards to the real bus-alert.sh.
# Generated fresh into the scratch dir each run; gone with it at cleanup.
# ---------------------------------------------------------------------------
WRAPPER="$SCRATCH_DIR/bus-alert-wrapper.sh"
{
  printf '#!/usr/bin/env bash\n'
  printf '# Generated by scheduled-review-preflight.sh for the %s run only.\n' "$DATE"
  printf 'set -uo pipefail\n'
  printf 'SUBJECT="$1"\n'
  printf 'BODY="$2"\n'
  printf 'SLUG="${3:-alert}"\n'
  printf 'NOTE="Nothing was published: the real Decisions archive and the reMarkable device were not touched. This is the %s pre-flight only; the 05:00 scheduled-review run is unaffected and still runs independently."\n' "$DATE"
  printf 'exec "%s" "Decisions PRE-FLIGHT: $SUBJECT" "$BODY\n\n$NOTE" "preflight-$SLUG"\n' "$REAL_BUS_ALERT"
} > "$WRAPPER"
chmod +x "$WRAPPER"

# seed_fail <reason> - the one path every fidelity-seed failure routes
# through (the cross-reviewer's review, blocker 2). An EXISTING source file that
# cannot be listed, read or copied must stop the run and alert, never seed
# a partial or silently-wrong history and report clean.
seed_fail() {
  local reason="$*"
  log "$DATE: FIDELITY SEED FAILURE: $reason"
  local subject="Fidelity seed FAILED for $DATE"
  local body="scheduled-review-preflight.sh could not seed the scratch archive with the real answer/question history for $DATE, so the run was stopped rather than risk reviewing a different card set from what the 05:00 job would render.

$reason"
  if "$WRAPPER" "$subject" "$body" "seed-failed-$DATE" >>"$LOG_FILE" 2>&1; then
    log "$DATE: fidelity-seed-failure alert sent to the orchestrator"
  else
    log "$DATE: fidelity-seed-failure alert failed to send; see log above"
  fi
  exit 1
}

# ---------------------------------------------------------------------------
# Fidelity seed (mc-t5mb6 cross-review finding). An EMPTY scratch archive is
# not neutral: generate() (today_page.py) reads answer/question history out
# of ANSWER_RECORD_ROOT and QUESTION_STATE_PATH - both now pointed at this
# scratch tree by the REMARKABLE_TODAY_ARCHIVE override below - to decide
# which queue rows are already answered or settled
# (filter_already_answered / load_recorded_answers / _record_archived_answers
# at today_page.py ~1945-1953,1880-1942, and load_settled_question_history at
# ~2004-2100). A scratch tree with no history makes those filters see
# nothing, so this run could print a card the real 05:00 render would
# exclude - a false alarm, or a different card set entirely. Seeding
# READ-ONLY COPIES of exactly what those readers consume - never the PDFs -
# closes that gap without touching the real archive:
#   - every dated directory's *.manifest.json (load_settled_question_history
#     and its supplement lookup, _manifest_for_page_id, today_page.py ~1390-1413)
#   - every dated directory's processed/resolution.json
#     (_record_archived_answers, today_page.py ~1880-1942)
#   - question-state.json itself, one level above today-pages in the real
#     archive (QUESTION_STATE_PATH), copied to the same place relative to
#     this scratch archive (SCRATCH_DIR, its parent)
# A missing real archive or state file (a fresh checkout, nothing rendered
# yet) is an explicit, LOGGED allowed case, not a failure: generate() already
# tolerates an empty ANSWER_RECORD_ROOT/no question-state.json, and that is
# also just true on a machine that has never rendered anything. But once a
# source file DOES exist, failing to list, read or copy it must fail closed
# (the cross-reviewer's review, blocker 2) - the old silent-skip here could seed a
# partial history and report a clean run against the wrong card set.
SEED_SRC_ARCHIVE="$RMW_DECISIONS_ARCHIVE"
SEED_SRC_STATE="$RMW_ARCHIVE/question-state.json"
SEEDED_MANIFESTS=0
SEEDED_RESOLUTIONS=0

if [ -d "$SEED_SRC_ARCHIVE" ]; then
  MANIFEST_LIST="$SCRATCH_DIR/.seed-manifests.list"
  MANIFEST_ERR="$SCRATCH_DIR/.seed-manifests.err"
  if ! find "$SEED_SRC_ARCHIVE" -name '*.manifest.json' -print0 \
      >"$MANIFEST_LIST" 2>"$MANIFEST_ERR"; then
    seed_fail "could not list manifest files under $SEED_SRC_ARCHIVE: $(cat "$MANIFEST_ERR" 2>/dev/null)"
  fi
  while IFS= read -r -d '' manifest; do
    rel="${manifest#"$SEED_SRC_ARCHIVE"/}"
    dest="$SCRATCH_ARCHIVE/$rel"
    mkdir -p "$(dirname "$dest")"
    if ! cp "$manifest" "$dest" 2>"$SCRATCH_DIR/.seed-cp.err"; then
      seed_fail "could not copy $manifest into the scratch archive: $(cat "$SCRATCH_DIR/.seed-cp.err" 2>/dev/null)"
    fi
    chmod 444 "$dest" 2>/dev/null || true
    SEEDED_MANIFESTS=$((SEEDED_MANIFESTS + 1))
  done < "$MANIFEST_LIST"

  RESOLUTION_LIST="$SCRATCH_DIR/.seed-resolutions.list"
  RESOLUTION_ERR="$SCRATCH_DIR/.seed-resolutions.err"
  if ! find "$SEED_SRC_ARCHIVE" -path '*/processed/resolution.json' -print0 \
      >"$RESOLUTION_LIST" 2>"$RESOLUTION_ERR"; then
    seed_fail "could not list resolution.json files under $SEED_SRC_ARCHIVE: $(cat "$RESOLUTION_ERR" 2>/dev/null)"
  fi
  while IFS= read -r -d '' resolution; do
    rel="${resolution#"$SEED_SRC_ARCHIVE"/}"
    dest="$SCRATCH_ARCHIVE/$rel"
    mkdir -p "$(dirname "$dest")"
    if ! cp "$resolution" "$dest" 2>"$SCRATCH_DIR/.seed-cp.err"; then
      seed_fail "could not copy $resolution into the scratch archive: $(cat "$SCRATCH_DIR/.seed-cp.err" 2>/dev/null)"
    fi
    chmod 444 "$dest" 2>/dev/null || true
    SEEDED_RESOLUTIONS=$((SEEDED_RESOLUTIONS + 1))
  done < "$RESOLUTION_LIST"
fi

SEEDED_STATE=0
if [ -f "$SEED_SRC_STATE" ]; then
  if ! cp "$SEED_SRC_STATE" "$SCRATCH_DIR/question-state.json" 2>"$SCRATCH_DIR/.seed-cp.err"; then
    seed_fail "could not copy $SEED_SRC_STATE into the scratch archive: $(cat "$SCRATCH_DIR/.seed-cp.err" 2>/dev/null)"
  fi
  chmod 444 "$SCRATCH_DIR/question-state.json" 2>/dev/null || true
  SEEDED_STATE=1
fi

log "$DATE: fidelity seed - $SEEDED_MANIFESTS manifest(s), $SEEDED_RESOLUTIONS resolution.json, question-state.json seeded=$SEEDED_STATE"

# ---------------------------------------------------------------------------
# Bus-alert CAPTURE stub. scheduled-review.sh's own fail() path is pointed
# at this instead of the real wrapper above, so its alert is captured to
# files rather than sent - this script composes the one combined alert
# itself, below, after it also knows whether any rows were HELD (the cross-reviewer's
# review, blocker 1).
# ---------------------------------------------------------------------------
CAPTURE_SUBJECT="$SCRATCH_DIR/captured-alert-subject.txt"
CAPTURE_BODY="$SCRATCH_DIR/captured-alert-body.txt"
CAPTURE_SCRIPT="$SCRATCH_DIR/bus-alert-capture.sh"
{
  printf '#!/usr/bin/env bash\n'
  printf 'set -uo pipefail\n'
  printf 'printf %%s "$1" > %s\n' "$(printf '%q' "$CAPTURE_SUBJECT")"
  printf 'printf %%s "$2" > %s\n' "$(printf '%q' "$CAPTURE_BODY")"
  printf 'exit 0\n'
} > "$CAPTURE_SCRIPT"
chmod +x "$CAPTURE_SCRIPT"

# ---------------------------------------------------------------------------
# Run scheduled-review.sh, unmodified, through its own environment
# overrides. RUN_LOG is fresh every run (never appended-to across days) so
# the HELD-line extraction below only ever sees THIS run's output; its
# content is merged into the durable LOG_FILE right after.
# ---------------------------------------------------------------------------
RUN_LOG="$SCRATCH_DIR/scheduled-review.log"
: > "$RUN_LOG"

# mc-0kpzk: scheduled-review.sh's own kept-reviewer-log default lives under
# its $TMP, which for this run is SCRATCH_TMP inside SCRATCH_DIR - removed
# unconditionally on exit (the trap above). Point it at this script's own
# KEEP_DIR instead, named like the other artifacts already kept there, so a
# reviewer-related failure here leaves the same evidence a failure under the
# real 05:00 job would.
KEPT_REVIEWER="$KEEP_DIR/scheduled-review-preflight-$DATE-reviewer.log"

REMARKABLE_TODAY_ARCHIVE="$SCRATCH_ARCHIVE" \
SCHEDULED_REVIEW_LOCK="$INNER_LOCK" \
SCHEDULED_REVIEW_LOG="$RUN_LOG" \
SCHEDULED_REVIEW_TMP="$SCRATCH_TMP" \
SCHEDULED_REVIEW_BUS_ALERT="$CAPTURE_SCRIPT" \
SCHEDULED_REVIEW_REVIEWER_KEEP_PATH="$KEPT_REVIEWER" \
  "$SCHEDULED_REVIEW" --date "$DATE"
RC=$?

cat "$RUN_LOG" >> "$LOG_FILE" 2>/dev/null || true

# ---------------------------------------------------------------------------
# HELD: extraction (mc-t5mb6 design point 2). verify-queue.py prints
# "  HELD: <title> -> <reasons>" to stderr for every card-contract row it
# holds, and scheduled-review.sh's own log capture already carries that text
# into RUN_LOG whether the review passes or fails. A held row disappears
# from the document silently, so it is reported regardless of outcome.
# ---------------------------------------------------------------------------
HELD_LINES="$(grep -E '^[[:space:]]*HELD: ' "$RUN_LOG" 2>/dev/null || true)"

# ---------------------------------------------------------------------------
# Keep evidence for the author before the scratch dir goes away. The
# rendered PDF, if one was produced; and the closest available proxy for
# "the reviewer's failed receipt" - review-receipt.py's own check output,
# which names every card and every one of the reader's three tests it failed
# (remarkable/review-receipt.py check_receipt()). The staged receipt
# itself never survives past scheduled-review.sh's own return (it is removed
# inside STAGE_DIR by that script's fail() path before this script regains
# control, by design - mc-mi8xx hardening B1/B4), so the check log is the
# real artifact available here.
# ---------------------------------------------------------------------------
PDF_SRC="$SCRATCH_ARCHIVE/$DATE/decisions-$DATE.pdf"
KEPT_PDF="$KEEP_DIR/scheduled-review-preflight-$DATE.pdf"
MANIFEST_SRC="$SCRATCH_ARCHIVE/$DATE/decisions-$DATE.manifest.json"
KEPT_MANIFEST="$KEEP_DIR/scheduled-review-preflight-$DATE.manifest.json"
CHECK_LOG_SRC="$SCRATCH_TMP/scheduled-review-check-$DATE.log"
KEPT_RECEIPT="$KEEP_DIR/scheduled-review-preflight-$DATE-receipt-check.log"

PDF_KEPT=0
MANIFEST_KEPT=0
RECEIPT_KEPT=0
if [ -f "$PDF_SRC" ] && cp "$PDF_SRC" "$KEPT_PDF" 2>/dev/null; then
  PDF_KEPT=1
fi
# Kept alongside the PDF so the card_ids it lists are available for a parity
# check against a plain render of the same date without needing to keep the
# scratch archive itself around (mc-t5mb6 cross-review, fix 1).
if [ -f "$MANIFEST_SRC" ] && cp "$MANIFEST_SRC" "$KEPT_MANIFEST" 2>/dev/null; then
  MANIFEST_KEPT=1
fi
if [ -f "$CHECK_LOG_SRC" ] && cp "$CHECK_LOG_SRC" "$KEPT_RECEIPT" 2>/dev/null; then
  RECEIPT_KEPT=1
fi

EXIT_CODE=0

if [ "$RC" -ne 0 ]; then
  EXIT_CODE=1
  log "$DATE: scheduled-review.sh exited $RC under the pre-flight wrapper"
  # ONE combined alert (the cross-reviewer's review, blocker 1): scheduled-review.sh's
  # own alert was captured, not sent, above - compose it here, with any
  # card-contract HELD rows appended, so a single message names both. A
  # HELD row disappears from the document silently even on a failing run,
  # so it must not be dropped just because the run also failed for another
  # reason.
  if [ -f "$CAPTURE_SUBJECT" ] && [ -f "$CAPTURE_BODY" ]; then
    CAPTURED_SUBJECT="$(cat "$CAPTURE_SUBJECT")"
    CAPTURED_BODY="$(cat "$CAPTURE_BODY")"
  else
    CAPTURED_SUBJECT="Scheduled review FAILED for $DATE"
    CAPTURED_BODY="scheduled-review.sh exited $RC but its own alert was not captured (see $LOG_FILE for what it logged)."
  fi
  if [ -n "$HELD_LINES" ]; then
    log "$DATE: card-contract hold(s) also present; combined into the one alert below"
    COMBINED_BODY="$CAPTURED_BODY

Card-contract hold(s) also present for $DATE (a held card disappears from the document silently unless fixed before 05:00):

$HELD_LINES"
  else
    COMBINED_BODY="$CAPTURED_BODY"
  fi
  if "$WRAPPER" "$CAPTURED_SUBJECT" "$COMBINED_BODY" "failed-$DATE" >>"$LOG_FILE" 2>&1; then
    log "$DATE: combined failure alert sent to the orchestrator"
  else
    log "$DATE: combined failure alert failed to send; see log above"
  fi
elif [ -n "$HELD_LINES" ]; then
  EXIT_CODE=1
  log "$DATE: review passed, but the card contract held row(s) - a held card silently disappears from the document unless fixed before 05:00"
  BODY="scheduled-review-preflight.sh (03:30) rendered and reviewed $DATE cleanly, but the card contract held the following row(s). A held card disappears from the document silently unless fixed before 05:00.

$HELD_LINES

Full pre-flight log: $LOG_FILE"
  if "$WRAPPER" "Held card(s) for $DATE" "$BODY" "held-$DATE" >>"$LOG_FILE" 2>&1; then
    log "$DATE: held-card alert sent to the orchestrator"
  else
    log "$DATE: held-card alert failed to send; see log above"
  fi
else
  log "$DATE: clean - review passed, card contract held nothing; no alert sent"
fi

[ "$PDF_KEPT" -eq 1 ] && log "$DATE: rendered PDF kept at $KEPT_PDF"
[ "$MANIFEST_KEPT" -eq 1 ] && log "$DATE: manifest kept at $KEPT_MANIFEST"
[ "$RECEIPT_KEPT" -eq 1 ] && log "$DATE: reviewer's receipt-check log kept at $KEPT_RECEIPT"
# mc-0kpzk: scheduled-review.sh itself already logs "reviewer log kept at
# $KEPT_REVIEWER" into RUN_LOG on a reviewer-related failure (timeout,
# nonzero exit, bad receipt); this just names it in the same summary block
# as the other kept artifacts for a reader scanning the tail of the log.
# Checking bare existence of $KEPT_REVIEWER here would be wrong (the cross-reviewer's
# review of bd3f1b21): its path is deterministic per DATE, so a failing run
# followed by a later clean rerun for the SAME date would still find the
# EARLIER run's file sitting there and report it as if this run had just
# kept it. RUN_LOG is fresh every run (": > "$RUN_LOG"" above), so grepping
# it for the line THIS run's own child process actually wrote is what keys
# this to this run's real outcome, not to leftover disk state.
grep -qF "reviewer log kept at $KEPT_REVIEWER" "$RUN_LOG" 2>/dev/null && \
  log "$DATE: reviewer log kept at $KEPT_REVIEWER"

exit "$EXIT_CODE"
