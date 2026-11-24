#!/usr/bin/env bash
# Deterministic reMarkable annotation detection.
# A launchd timer (<label_prefix>.annotation-watch, every 120 s) runs the
# detector and, ONLY when a change is found, runs the deterministic Decisions
# resolver and then the on_annotation hook, naming the changed device paths and
# the handling protocol. Deterministic detection, state-transition alerting; a
# model, if one is wired to the hook, does judgment only (pull, vision-read, execute clear pen answers per mc-nu3e as amended
# 2026-07-18: direct execution, ask-first only for ambiguous/contradicting/irreversible).
#
# The detector (check-annotations.sh) advances its own baseline on every run. That is
# safe when the on_annotation hook is durable (a queue or message bus): once the message
# is accepted, the detection cannot be lost even if the receiving agent is busy. A
# failed hook restores the baseline so the next run retries.
# Origin: 2026-07-12 08:19 - a busy heartbeat turn dropped the checklist probe while
# The reader's pen answers sat on the device.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Load the config into RMW_* variables. A copy of this script outside the
# repository (a test harness) falls back to RMW_PROJECT_ROOT, then to the
# built-in defaults below.
if [ -f "$DIR/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$DIR/rmw-env.sh"
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
LOG=/tmp/remarkable-annotation-watch.log

# mc-rsh8 (2026-08-23): this watcher runs every 2 minutes and was the one
# high-frequency caller hitting the cloud UNWRAPPED, so 429 storms killed its
# runs and every sibling tool. Route every rmapi call through the retry
# wrapper. today_watch.py reads REMARKABLE_RMAPI (not RMAPI) - set both, or
# it silently uses the raw binary (the env-var mismatch was a live bug).
export RMAPI="$DIR/rmapi-retry.sh"
export REMARKABLE_RMAPI="$RMAPI"

# Serialize watcher transactions. On a refused send, restore the detector's
# baseline so the next scheduled run sees and retries the same ink change.
umask 077
exec 8>"$HOME/.remarkable-annotation-watch.lock" || exit 1
/usr/bin/lockf -t 0 8 || exit 1
BASELINE_PATHS=("$HOME/.remarkable-annotation-state.json"
  "${RM_ARCHIVE_STAMP:-$HOME/.remarkable-annotation-state.json.archive-stamp}")
TRANSACTION=$(mktemp -d "$HOME/.remarkable-annotation-watch.XXXXXXXX") || exit 1
BASELINE_COMMITTED=0
for index in "${!BASELINE_PATHS[@]}"; do
  if [ -f "${BASELINE_PATHS[$index]}" ]; then
    cp -p "${BASELINE_PATHS[$index]}" "$TRANSACTION/$index" || { rm -rf "$TRANSACTION"; exit 1; }
  fi
done
finish_transaction() {
  local status=$? index
  if [ "$BASELINE_COMMITTED" -eq 0 ]; then
    for index in "${!BASELINE_PATHS[@]}"; do
      if [ -f "$TRANSACTION/$index" ]; then
        mv -f "$TRANSACTION/$index" "${BASELINE_PATHS[$index]}" || status=1
      else
        rm -f "${BASELINE_PATHS[$index]}" || status=1
      fi
    done
  fi
  rm -rf "$TRANSACTION"
  return "$status"
}
trap finish_transaction EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
CHANGED=$("$DIR/check-annotations.sh" 2>/dev/null) || exit 1
if [ -z "$CHANGED" ]; then
  BASELINE_COMMITTED=1
  exit 0
fi

# Registered Today pages run through the deterministic manifest resolver first.
# With no registry or no matching path, this is a no-op. Registry entries default
# to dry-run unless their apply field is exactly true.
TODAY_RESULT=$(CHANGED="$CHANGED" "${RMW_RENDER_PYTHON:-python3}" \
  "$DIR/today_watch.py" 2>>"$LOG")
# mc-1jw9 defect 2: this exit code used to be discarded here - today_watch.py
# could fail (or exit non-zero for a blocked/needs-review/no-ink-read
# process-archive outcome) and this script still exited 0, so neither a
# direct run nor the launchd timer ever surfaced the failure. Capture it and
# propagate it as this script's own exit at the bottom.
TODAY_STATUS=$?
if [ "$TODAY_STATUS" -ne 0 ]; then
  echo "$(date +%Y-%m-%dT%H:%M:%S%z) today_watch.py exited $TODAY_STATUS" >> "$LOG"
fi

CHANGED="$CHANGED" TODAY_RESULT="$TODAY_RESULT" "${RMW_RENDER_PYTHON:-python3}" - "$DIR" <<'EOF'
import os, sys, datetime
sys.path.insert(0, sys.argv[1])
import rmw_config

changed = [l for l in os.environ["CHANGED"].splitlines() if l.strip()]
today_result = os.environ.get("TODAY_RESULT", "")

body = (
    "reMarkable annotations changed on: " + ", ".join(changed) + "\n\n"
    "Handling protocol (mc-nu3e, updated per the reader's 2026-07-18 graduation): for each "
    "path, `rmapi geta \"/<path>\"`, Read the annotations PDF, vision-read the pen "
    "marks, then EXECUTE clear pen answers directly (record the disposition, then act; "
    "report what was read and done). Ask first only if the ink is ambiguous or "
    "unreadable, contradicts a prior recorded decision, or authorizes an irreversible "
    "or external-facing action. The detector baseline has already advanced - this "
    "message is the only record of the change; handle it now. NOTE (mc-k1tr): this "
    "alert fired because the doc carries the reader's 'ready' tag - his done-annotating "
    "signal. Untagged ink changes are quiet-logged, never alerted. CLOSING STEP "
    "(corrected 2026-09-04, mc-1nr9d): once EVERY mark on a doc is dispositioned, run "
    "remarkable/unset-ready-tag.sh \"/<path>\" to record it as processed. This no "
    "longer clears the 'ready' tag or touches the document on the device in any way - "
    "it only records the ink hash on our side, in ~/.remarkable-processed-docs.tsv. The "
    "doc and its tag stay exactly as the reader left them, in place, forever, until he changes "
    "them himself. Skip this on partially processed docs."
)
if today_result:
    body += (
        "\n\nThe registered Today-page handler also produced this deterministic result: "
        + today_result
        + "\nDo not vision-read or execute the same Today-page boxes again. Read the "
        "saved resolution artifact for decisions and ambiguities, report completed "
        "receipts, and ask about marks whose status is needs_review. If the result "
        "carries a non-empty note_regions list, each entry names a PNG (path) of "
        "handwritten note-line ink cropped for vision-read (mc-1jw9) - read every one; "
        "nothing handwritten in those regions was auto-interpreted."
    )
subject = f"reMarkable annotations changed: {changed[0]}" + (" (+more)" if len(changed) > 1 else "")
# Delivery goes to hooks.on_annotation: subject, body, then each changed path.
# With no hook configured the change is only logged; the deterministic
# Decisions resolver above has already run either way. See
# hooks/examples/agent-bus-send.py for a durable HTTP message-bus hook (the
# original deployment sent this to an orchestrating agent that pulls and
# vision-reads the ink).
outcome = rmw_config.run_hook("on_annotation", subject, body, *changed, timeout=30)
if not outcome.get("configured"):
    status = "logged (no on_annotation hook configured)"
elif outcome.get("sent"):
    status = "sent"
else:
    status = f"send-failed: {outcome.get('error', '')}"
with open("/tmp/remarkable-annotation-watch.log", "a") as f:
    f.write(f"{datetime.datetime.now().isoformat()} {','.join(changed)} {status}\n")
raise SystemExit(1 if status.startswith("send-failed") else 0)
EOF
SEND_STATUS=$?
if [ "$SEND_STATUS" -ne 0 ]; then
  echo "Annotation delivery failed; the detector baseline will be restored." >&2
  exit 1
fi
BASELINE_COMMITTED=1

# mc-1jw9 defect 2: propagate today_watch.py's exit code as this script's own,
# so a direct run and the launchd timer report the SAME failure identically -
# the generic "annotations changed" alert above still always fires regardless.
exit "$TODAY_STATUS"
