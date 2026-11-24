#!/usr/bin/env bash
# mc-6t2o daily 06:00 delivery job. REWORKED 2026-07-28 per the reader's consensus:
# TWO morning documents. (1) Morning Briefing = overnight prose + status block
# (calendar, service health, weather). (2) Decisions = full-card decision
# document. Scheduled by launchd: <label_prefix>.morning-delivery.
#
# REWORKED AGAIN 2026-08-20 (mc-p9xd) after three silent mornings. On 08-17,
# 08-18 and 08-19 no overnight handoff existed, so the briefing step exited the
# whole script and the Decisions document was never even attempted; the only
# notice went to Telegram, retired as the reader's inbound channel on 2026-07-29. The reader
# found out by asking. Four rules now govern this file:
#
#   1. EVERY deliverable is attempted. No step exits the script. Each records an
#      outcome and the run continues; the exit code is the worst outcome seen.
#   2. The Decisions document runs BEFORE the briefing, precisely because the
#      briefing may legitimately wait up to ~45 minutes for a late handoff
#      (mc-4asp). Decisions must never sit behind that wait.
#   3. EXACTLY ONE outcome line goes to the reader by iMessage every run, success or
#      failure. Component-level detail goes to this log and to the bus, never to
#      his phone. If the iMessage itself fails, it is recorded durably and
#      surfaced on the next run instead of vanishing.
#   4. NOTHING LEAVES THIS SCRIPT UNLESS EXPLICITLY ENABLED. Every message,
#      alert and device write goes through the single outbound() guard below,
#      which is OFF unless MB_ALLOW_OUTBOUND=1. An unset flag means DO NOT SEND.
#
# ==> DEPLOYMENT REQUIREMENT: the launchd job MUST set MB_ALLOW_OUTBOUND=1 in its
#     EnvironmentVariables. Without it this script delivers nothing, says so
#     loudly in the log, and exits non-zero so the misconfiguration is visible.
#     (Origin 2026-08-20: a test run of this script reached the real agent bus at
#     02:48 because the outbound paths were individually gated instead of being
#     funnelled through one default-off guard.)
#
# "No overnight handoff" is a REPORTED STATE, not a reason to emit nothing. A
# stub handoff is never manufactured to satisfy the freshness gate; that gate
# exists to stop false delivery.
#
# MB_DRY_RUN=1 additionally forces outbound off and keeps the exit code honest,
# so a deliberate rehearsal is clean while a misdeployment is not.
# Every external dependency is overridable so the failure paths can be exercised
# against this real script (remarkable/test-morning-outcome.sh).

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

TODAY="${MB_DATE:-$(date +%F)}"
DRY_RUN="${MB_DRY_RUN:-0}"

TMP="${MB_TMP_DIR:-/tmp}"           # all scratch lives here; tests point it elsewhere
RMAPI_REAL_BIN="${RMAPI_REAL:-${RMAPI:-$(command -v "${RMW_TABLET_RMAPI:-rmapi}" 2>/dev/null || echo "${RMW_TABLET_RMAPI:-rmapi}")}}"
RMAPI="${MB_RMAPI_RETRY:-$DIR/rmapi-retry.sh}"
export RMAPI RMAPI_REAL="$RMAPI_REAL_BIN"
# Where the overnight briefing file lives (briefing.source_dir).
REPO="${MB_REPO:-$RMW_BRIEFING_SOURCE_DIR}"
GENERATE_PY="${MB_GENERATE:-$DIR/generate-dashboard.py}"
PUSH_BRIEF="${MB_PUSH_BRIEF:-$DIR/push-brief.sh}"
PUSH_DECISIONS="${MB_PUSH_DECISIONS:-$DIR/push-decisions.sh}"
VERIFY_QUEUE="${MB_VERIFY_QUEUE:-$DIR/verify-queue.py}"
ROTATE="${MB_ROTATE:-$DIR/rotate-briefs.sh}"
# Operator alerts (hooks.alert) and the one reader line (hooks.notify).
BUS_ALERT="${MB_BUS_ALERT:-$DIR/hooks/alert.sh}"
IMESSAGE_ALERT="${MB_IMESSAGE_ALERT:-$DIR/hooks/notify.sh}"
ARCHIVE_ROOT="${REMARKABLE_TODAY_ARCHIVE:-$RMW_DECISIONS_ARCHIVE}"
BRIEF_ARCHIVE="${MB_BRIEF_ARCHIVE:-$RMW_ARCHIVE}"
PENDING="${MB_PENDING_FILE:-$HOME/.morning-outcome-pending}"
OUTCOME_RECORD_DIR="${MB_OUTCOME_RECORD_DIR:-$HOME/.morning-outcomes}"
OUTCOME_RECORD="$OUTCOME_RECORD_DIR/$TODAY.json"
AGENT_ALERT_MARKER_DIR="${MB_AGENT_ALERT_MARKER_DIR:-$HOME/.morning-agent-alerts}"
DEST="${MB_DEST:-${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}}"
PROBE_DEST="${MB_PROBE_DEST:-/Probes}"
RETRIES="${MB_RETRIES:-9}"          # 9 x 5min = waits until ~06:45 for a late handoff
RETRY_DELAY="${MB_RETRY_DELAY:-300}"

mkdir -p "$TMP"

echo "=== morning-dashboard run $(date '+%F %T') for $TODAY ==="

# ---------------------------------------------------------------------------
# THE OUTBOUND GUARD. This is the ONLY way anything leaves this script: every
# iMessage, every bus alert, every device write and every push runs as
# `outbound <label> <command...>`. Default is OFF. A test, a rehearsal or a
# forgotten flag therefore sends nothing; it cannot.
# ---------------------------------------------------------------------------
SUPPRESSED=90                       # distinct rc so callers can tell "not sent" from "failed"
OUTBOUND_ENABLED=0
[ "${MB_ALLOW_OUTBOUND:-0}" = "1" ] && OUTBOUND_ENABLED=1
[ "$DRY_RUN" = "1" ] && OUTBOUND_ENABLED=0
OUTBOUND_SUPPRESSED_COUNT=0

note() { echo "[$(date '+%T')] $*"; }

outbound_on() { [ "$OUTBOUND_ENABLED" = "1" ]; }

outbound() {
  local label="$1"; shift
  if ! outbound_on; then
    OUTBOUND_SUPPRESSED_COUNT=$((OUTBOUND_SUPPRESSED_COUNT + 1))
    note "OUTBOUND SUPPRESSED [$label]: $1"
    return "$SUPPRESSED"
  fi
  "$@"
}

if outbound_on; then
  note "outbound ENABLED: this run may write to the device and message the reader"
elif [ "$DRY_RUN" = "1" ]; then
  note "dry run: outbound disabled, nothing will be sent or written to the device"
else
  note "OUTBOUND DISABLED: MB_ALLOW_OUTBOUND is not set, so this run delivers NOTHING."
  note "  If this is the scheduled 06:00 job, its launchd entry is missing MB_ALLOW_OUTBOUND=1."
fi

# ---------------------------------------------------------------------------
# Outcome state. Severity: 0 all good, 1 degraded (a document still landed),
# 2 a document did not land. The run always reaches the end; WORST is the exit.
# ---------------------------------------------------------------------------
OK=0; SOFT=1; HARD=2
WORST=$OK
bump() { [ "$1" -gt "$WORST" ] && WORST="$1"; return 0; }

CAL_PHRASE="calendar unavailable (not checked)"
PROBE_PHRASE="tablet write test did not run"
BRIEF_PHRASE="briefing withheld (the run did not reach it)"
DEC_PHRASE="decisions withheld (the run did not reach it)"

# Component detail goes to this log and to the bus. NEVER to the reader's phone.
# The helper's own chatter is silenced INSIDE this wrapper, so that the guard's
# "OUTBOUND SUPPRESSED" line still reaches the log when a run is not allowed out.
# shellcheck disable=SC2329  # invoked indirectly, by name, through outbound()
quiet_bus_alert() {
  # BUS_ALERT_RECIPIENTS is read by a message-bus alert hook (see
  # hooks/examples/agent-bus-send.py); other hooks ignore it.
  BUS_ALERT_RECIPIENTS="${MB_BUS_ALERT_RECIPIENTS:-operators}" \
    "$BUS_ALERT" "$@" >/dev/null 2>&1
}
bus_note() {
  # bus_note <subject> <body> <slug>; never fatal, never blocks the run.
  [ -x "$BUS_ALERT" ] || { note "bus alert unavailable ($BUS_ALERT not executable)"; return 0; }
  local marker=""
  if outbound_on; then
    mkdir -p "$AGENT_ALERT_MARKER_DIR" 2>/dev/null || true
    chmod 700 "$AGENT_ALERT_MARKER_DIR" 2>/dev/null || true
    marker="$AGENT_ALERT_MARKER_DIR/$TODAY-$3.pending"
    (umask 077; printf '%s\n%s\n%s\n' "rmw-morning-agent-alert-v1" "$1" "$2" > "$marker.tmp") 2>/dev/null \
      && mv "$marker.tmp" "$marker" 2>/dev/null || true
  fi
  outbound "bus alert" quiet_bus_alert "$1" "$2" "$3"
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    [ -n "$marker" ] && rm -f "$marker"
  elif [ "$rc" -ne "$SUPPRESSED" ]; then
    note "bus alert failed for: $1; durable marker retained"
  fi
  return 0
}

recover_agent_alerts() {
  local marker schema subject body slug marker_size
  outbound_on || return 0
  [ -d "$AGENT_ALERT_MARKER_DIR" ] || return 0
  for marker in "$AGENT_ALERT_MARKER_DIR"/*.pending; do
    [ -f "$marker" ] || continue
    marker_size="$(wc -c < "$marker" 2>/dev/null)" || marker_size=0
    schema="$(sed -n '1p' "$marker")"
    if [ -L "$marker" ] || [ "$marker_size" -gt 16384 ] \
      || [ "$schema" != "rmw-morning-agent-alert-v1" ]; then
      note "REFUSING unsafe pending bus alert marker: $marker"
      continue
    fi
    subject="$(sed -n '2p' "$marker")"
    body="$(sed -n '3,$p' "$marker")"
    slug="$(basename "$marker" .pending)"
    if outbound "pending bus alert" quiet_bus_alert "$subject" "$body" "$slug"; then
      rm -f "$marker"
      note "bus alert recovery sent: $subject"
    else
      note "bus alert recovery still failing: $subject"
    fi
  done
}

recover_agent_alerts

# A prior write-ahead marker means an earlier iMessage attempt has an uncertain
# outcome. Surface it to the agents, never by sending an old line to the reader.
if [ -s "$PENDING" ]; then
  bus_note "Earlier morning outcome needs review ($TODAY)" \
    "A previous morning outcome attempt left its write-ahead marker. Check the Messages database and the outcome record. This run will send only its own current line." \
    "morning-outcome-pending"
fi

# ---------------------------------------------------------------------------
# 1. Calendar. A read, not a send: it fetches the reader's own calendar and writes a
# local file, so it is deliberately NOT behind the outbound guard. It is best
# effort and never blocks a document.
# ---------------------------------------------------------------------------
CAL="$TMP/morning-dashboard-calendar-$TODAY.md"
CAL_ERR="$TMP/morning-dashboard-calendar-err-$TODAY.txt"
CAL_ARG=()
TMIN=$(date -v0H -v0M -v0S "+%Y-%m-%dT%H:%M:%S%z" | sed 's/\(..\)$/:\1/')
TMAX=$(date -v23H -v59M -v59S "+%Y-%m-%dT%H:%M:%S%z" | sed 's/\(..\)$/:\1/')
# Calendar source: briefing.calendar_command (RMW_BRIEFING_CALENDAR_COMMAND, a
# JSON argv list) first, then the `gws` Google Workspace CLI when it is on PATH.
# Both must print Google Calendar style JSON ({"items": [...]}).
CAL_JSON="$(mktemp)"
# Writes the events JSON to $CAL_JSON. Notes stay out of the pipe: a note printed
# inside a pipeline would reach jq as input (observed 2026-09-06, broken pipe).
fetch_calendar_json() {
  local cal_cmd=() arg
  if [ -n "${RMW_BRIEFING_CALENDAR_COMMAND:-}" ] && [ "${RMW_BRIEFING_CALENDAR_COMMAND}" != "[]" ]; then
    while IFS= read -r -d '' arg; do cal_cmd+=("$arg"); done < <(
      python3 -c 'import json,sys; sys.stdout.write("\0".join(json.loads(sys.argv[1])) + "\0")' "$RMW_BRIEFING_CALENDAR_COMMAND")
    if "${cal_cmd[@]}" --time-min "$TMIN" --time-max "$TMAX" >"$CAL_JSON" 2>"$CAL_ERR"; then
      note "calendar: source briefing.calendar_command"
      return 0
    fi
    note "calendar: configured command failed, trying gws ($(head -c 200 "$CAL_ERR" 2>/dev/null | tr '\n' ' '))"
  fi
  command -v gws >/dev/null 2>&1 || { echo "no calendar source configured" > "$CAL_ERR"; return 1; }
  gws calendar events list --params "{\"calendarId\":\"primary\",\"timeMin\":\"$TMIN\",\"timeMax\":\"$TMAX\",\"singleEvents\":true,\"orderBy\":\"startTime\"}" >"$CAL_JSON" 2>"$CAL_ERR"
}
if fetch_calendar_json && jq -r 'if (.items|length) == 0 then "_No events on todays calendar._"
           else (.items[] |
             if .start.dateTime then
               "- **" + (.start.dateTime | sub("^.*T"; "") | sub(":[0-9]{2}[+-].*$"; "")) + "** " + (.summary // "(untitled)")
             else
               "- **all day** " + (.summary // "(untitled)")
             end)
           end' < "$CAL_JSON" > "$CAL" 2>/dev/null && [ -s "$CAL" ]; then
  CAL_ARG=(--calendar-file "$CAL")
  CAL_PHRASE="calendar available"
  note "calendar: fetched"
else
  rm -f "$CAL"
  if grep -qiE 'invalid_rapt|invalid_grant|reauth|401|unauthor' "$CAL_ERR" 2>/dev/null; then
    CAL_PHRASE="calendar unavailable (Google sign-in needs renewing)"
  else
    CAL_PHRASE="calendar unavailable (today's events could not be fetched)"
  fi
  note "calendar: UNAVAILABLE - $(head -c 300 "$CAL_ERR" 2>/dev/null | tr '\n' ' ')"
  bump "$SOFT"
  bus_note "Calendar unavailable for $TODAY" \
    "The 06:00 job could not fetch the reader's calendar. The local log has the diagnostic detail. Log: /tmp/morning-dashboard.log" \
    "morning-calendar-unavailable"
fi

# ---------------------------------------------------------------------------
# 2. reMarkable write probe. 2026-08-19: every rmapi write failed with HTTP 400
# because the cloud started rejecting an unsorted root index, and nothing noticed
# until a push was tried by hand. Prove a write works BEFORE the pushes, and
# clean up after: whatever the probe creates it removes, and if it cannot remove
# it, the outcome line says so.
# ---------------------------------------------------------------------------
minimal_pdf() {
  # A structurally valid one-page PDF with a real xref, ~350 bytes.
  python3 - "$1" <<'PYEOF'
import sys
objs = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
]
out = bytearray(b"%PDF-1.4\n")
offsets = []
for i, body in enumerate(objs, start=1):
    offsets.append(len(out))
    out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
start = len(out)
out += b"xref\n0 %d\n" % (len(objs) + 1)
out += b"0000000000 65535 f \n"
for off in offsets:
    out += b"%010d 00000 n \n" % off
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, start)
open(sys.argv[1], "wb").write(bytes(out))
PYEOF
}

run_probe() {
  if ! outbound_on; then
    PROBE_PHRASE="tablet write test skipped (outbound was disabled)"
    note "probe: skipped (outbound disabled)"
    return 0
  fi
  local work name pdf
  work="$(mktemp -d)"
  name="write-probe-$TODAY-$$"
  pdf="$work/$name.pdf"
  if ! minimal_pdf "$pdf" || [ ! -s "$pdf" ]; then
    PROBE_PHRASE="tablet write test could not start"
    note "probe: could not build the probe file"
    bump "$SOFT"
    rm -rf "$work"
    return 0
  fi

  outbound "probe mkdir" "$RMAPI" mkdir "$PROBE_DEST" >/dev/null 2>&1 || true
  if ! outbound "probe put" "$RMAPI" put "$pdf" "$PROBE_DEST" >/dev/null 2>&1; then
    PROBE_PHRASE="tablet write test failed"
    note "probe: WRITE FAILED (rmapi put rejected)"
    bump "$SOFT"
    bus_note "reMarkable writes are failing ($TODAY)" \
      "The 06:00 write probe could not upload a file to the device cloud. Expect both morning documents to fail until this clears. Log: /tmp/morning-dashboard.log" \
      "morning-rmapi-write-failed"
    rm -rf "$work"
    return 0
  fi

  # Remove what we created. Confirm it is gone at the real surface. The delete
  # is retried because the reMarkable cloud rate-limits back-to-back writes with
  # HTTP 429 (seen live 2026-08-20), and a rate-limited delete is not a leftover.
  outbound "probe rm" "$RMAPI" rm "$PROBE_DEST/$name" >/dev/null 2>&1 || true
  if "$RMAPI" ls "$PROBE_DEST" 2>/dev/null | awk -F'\t' '{print $2}' | grep -Fxq "$name"; then
    PROBE_PHRASE="tablet write test passed but cleanup failed"
    note "probe: WRITE OK but CLEANUP FAILED - $PROBE_DEST/$name remains"
    bump "$SOFT"
    bus_note "reMarkable write probe left a file behind ($TODAY)" \
      "The probe uploaded successfully but could not delete $PROBE_DEST/$name. Remove it by hand." \
      "morning-rmapi-probe-leftover"
  else
    PROBE_PHRASE="tablet write test passed"
    note "probe: write and cleanup both OK"
  fi
  rm -rf "$work"
  return 0
}
run_probe

# ---------------------------------------------------------------------------
# 3. Decisions document (mc-gx1o v2, full cards). Runs FIRST of the two
# documents: it must not queue behind the briefing's late-handoff wait, and it
# must be attempted no matter what the briefing does (mc-p9xd).
# ---------------------------------------------------------------------------
report_held_decisions() {
  DEC_PHRASE="your decisions are not ready yet; agents are working on them"
  note "decisions: EMPTY-DOC TRIPWIRE - document withheld"
  bump "$HARD"
  bus_note "Decisions doc WITHHELD for $TODAY" \
    "Active rows exist but no cards are ready. No PDF was rendered or pushed. Check the verification report. Log: /tmp/morning-dashboard.log" \
    "decisions-empty-doc"
}

# Phone question cards at delivery (mc-0mtb0): push-decisions.sh exit 7 means a
# question printed on today's document is open or already answered on the reader's
# phone, so the document was withheld for a fresh render and review. A check
# that did not run delivers, and the orchestrator is told. Both notes go to the bus only.
live_push_note() {
  local rc="$1" summary
  summary="$(python3 - "$ARCHIVE_ROOT/live-cards-decisions-$TODAY.json" "$rc" <<'PY' 2>/dev/null
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if sys.argv[2] == "7":
    if r.get("manifest_error"):
        print(f"Withheld: the delivered manifest is malformed ({r['manifest_error']}). Re-render and re-review "
              "today's document.")
    for h in r.get("withhold", []):
        print(f"Withheld: {h['headline']} (row {h['row_key']}, phone card {h['card_id']} is {h['status']}). "
              "Close or update the row, then re-render and re-review today's document.")
elif not r.get("checked"):
    print(f"Delivered without the phone-card check ({r.get('error')}).")
PY
)"
  if [ "$rc" -eq 7 ]; then
    DEC_PHRASE="decisions withheld (a question on it is already on your phone)"
    note "decisions: LIVE-CARD GATE - document withheld"
    bump "$HARD"
    bus_note "Decisions doc WITHHELD for $TODAY (phone question cards)" \
      "${summary:-push-decisions.sh exit 7: a printed question has a live phone card, or the check is missing. Log: /tmp/morning-dashboard.log}" \
      "decisions-live-cards-withheld"
  elif [ -n "$summary" ]; then
    bus_note "Decisions: phone question cards not checked at delivery ($TODAY)" "$summary" "decisions-live-cards-unchecked"
  fi
}

run_decisions() {
  if [ ! -x "$PUSH_DECISIONS" ]; then
    DEC_PHRASE="decisions withheld (the builder is not installed)"
    note "decisions: generator not installed at $PUSH_DECISIONS"
    bump "$HARD"
    return 0
  fi

  # Reviewed-document path (mc-iacbw section 6, 2026-09-02): if a non-producer
  # review receipt already exists for today, push exactly the reviewed bytes.
  # Re-rendering here would stamp a new verification time into the manifest
  # hash and the PDF, the receipt would no longer match, and the gate would
  # refuse a document that was in fact reviewed. Freshness was checked when
  # that document was rendered; the page says when.
  local reviewed_dir="$ARCHIVE_ROOT/$TODAY"
  if [ -f "$reviewed_dir/review-receipt.json" ] \
     && [ -f "$reviewed_dir/decisions-$TODAY.pdf" ] \
     && [ -f "$reviewed_dir/decisions-$TODAY.manifest.json" ]; then
    note "decisions: review receipt present for $TODAY; pushing the reviewed document without re-rendering"
    TODAY_EXTRACTOR_PYTHON="${TODAY_EXTRACTOR_PYTHON:-$RMW_RENDER_PYTHON}" \
    REMARKABLE_TODAY_ARCHIVE="$ARCHIVE_ROOT" \
      outbound "decisions push" "$PUSH_DECISIONS" --date "$TODAY" --no-generate --push
    rc=$?
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 7 ]; then
      live_push_note "$rc"
    fi
    if [ "$rc" -eq 7 ]; then
      :
    elif [ "$rc" -eq 0 ]; then
      DEC_PHRASE="decisions delivered"
      note "decisions: delivered (reviewed document)"
    elif [ "$rc" -eq 5 ]; then
      DEC_PHRASE="no decisions, so no document"
      note "decisions: nothing to ask today (empty queue)"
    elif [ "$rc" -eq 3 ]; then
      report_held_decisions
    elif [ "$rc" -eq "$SUPPRESSED" ]; then
      DEC_PHRASE="decisions withheld (outbound was disabled)"
      note "decisions: not pushed (outbound disabled)"
    else
      DEC_PHRASE="decisions withheld (delivery could not be verified)"
      note "decisions: PUSH FAILED (reviewed document)"
      bump "$HARD"
      bus_note "Decisions push FAILED for $TODAY" \
        "push-decisions.sh --no-generate failed during the 06:00 job; the reviewed document was not delivered. Log: /tmp/morning-dashboard.log" \
        "decisions-push-failed"
    fi
    return 0
  fi

  # Freshness pass (the reader 2026-07-28): nothing renders unverified. Held rows stay
  # off the document and are reported for orchestrator re-verification. This is a
  # local read-and-write, not an outbound action.
  local vqueue vreport rc
  vqueue="$TMP/verified-queue-$TODAY.md"
  vreport="$ARCHIVE_ROOT/verify-$TODAY.json"
  mkdir -p "$ARCHIVE_ROOT"
  if ! python3 "$VERIFY_QUEUE" --out-queue "$vqueue" --report "$vreport" ${PHONE_CARDS_URL:+--live-cards "$PHONE_CARDS_URL"}; then
    DEC_PHRASE="decisions withheld (the items could not be checked as current)"
    note "decisions: VERIFY FAILED - document withheld"
    bump "$HARD"
    bus_note "Decisions doc WITHHELD for $TODAY" \
      "verify-queue.py failed during the 06:00 job; the Decisions document was not generated. Log: /tmp/morning-dashboard.log" \
      "decisions-verify-failed"
    return 0
  fi
  # Phone question cards (mc-0mtb0): a row answered on the phone is held but
  # still has to be closed in REVIEW-QUEUE.md, and a check that did not run
  # held nothing. Both go to the orchestrator on the bus, never to the reader.
  local live_note
  live_note="$(python3 - "$vreport" <<'PY' 2>/dev/null
import json, sys
live = json.load(open(sys.argv[1])).get("live_cards")
if live is None:
    sys.exit(0)
lines = []
if not live.get("checked"):
    lines.append(f"Phone question cards were NOT checked ({live.get('error')}); no row was held for that check.")
for h in live.get("held", []):
    if h["status"] in ("answered", "discarded"):
        lines.append(f"{h['status'].capitalize()} on the phone, row not closed: {h['title']} "
                     f"(question {h['question_key']}, card {h['card_id']}). Close the row.")
for h in live.get("deferred", []):
    lines.append(f"Deferred on the phone, printed as usual: {h['title']} (question {h['question_key']}, "
                 f"card {h['card_id']}). Where a deferred question returns is still the reader's open choice.")
print("\n".join(lines))
PY
)"
  if [ -n "$live_note" ]; then
    bus_note "Decisions: phone question cards need attention ($TODAY)" "$live_note" "decisions-live-cards"
  fi

  TODAY_EXTRACTOR_PYTHON="${TODAY_EXTRACTOR_PYTHON:-$RMW_RENDER_PYTHON}" \
  REMARKABLE_TODAY_ARCHIVE="$ARCHIVE_ROOT" \
    outbound "decisions push" "$PUSH_DECISIONS" --date "$TODAY" --queue "$vqueue" --verify-report "$vreport" --push
  rc=$?
  if [ "$rc" -eq 0 ] || [ "$rc" -eq 7 ]; then
    live_push_note "$rc"
  fi
  if [ "$rc" -eq 7 ]; then
    :
  elif [ "$rc" -eq 0 ]; then
    DEC_PHRASE="decisions delivered"
    note "decisions: delivered"
  elif [ "$rc" -eq "$SUPPRESSED" ]; then
    DEC_PHRASE="decisions withheld (outbound was disabled)"
    note "decisions: not pushed (outbound disabled)"
  elif [ "$rc" -eq 2 ]; then
    DEC_PHRASE="decisions withheld (the document could not be generated)"
    note "decisions: GENERATION FAILED - document withheld"
    bump "$HARD"
    bus_note "Decisions doc WITHHELD for $TODAY" \
      "push-decisions.sh could not generate the Decisions document during the 06:00 job. Log: /tmp/morning-dashboard.log" \
      "decisions-generation-failed"
  elif [ "$rc" -eq 5 ]; then
    DEC_PHRASE="no decisions, so no document"
    note "decisions: nothing to ask today (empty queue)"
  elif [ "$rc" -eq 3 ]; then
    report_held_decisions
  elif [ "$rc" -eq 4 ]; then
    DEC_PHRASE="decisions withheld (the rendered document has no valid review receipt)"
    note "decisions: REVIEW GATE - document withheld"
    bump "$HARD"
    bus_note "Decisions doc WITHHELD for $TODAY" \
      "push-decisions.sh found no review receipt matching the rendered PDF and manifest (mc-iacbw gate). Re-run the independent review and receipt. Log: /tmp/morning-dashboard.log" \
      "decisions-review-gate"
  elif [ "$rc" -eq 6 ]; then
    DEC_PHRASE="decisions withheld (the generator crashed)"
    note "decisions: GENERATOR CRASHED - document withheld"
    bump "$HARD"
    bus_note "Decisions doc WITHHELD for $TODAY" \
      "today_page.py crashed during the 06:00 job. Log: /tmp/morning-dashboard.log" \
      "decisions-generation-failed"
  else
    DEC_PHRASE="decisions withheld (delivery could not be verified)"
    note "decisions: PUSH FAILED"
    bump "$HARD"
    bus_note "Decisions push FAILED for $TODAY" \
      "push-decisions.sh failed during the 06:00 job (mc-sguf class). Log: /tmp/morning-dashboard.log" \
      "decisions-push-failed"
  fi
  return 0
}
run_decisions

# ---------------------------------------------------------------------------
# 4. Morning Briefing (prose from the overnight handoff + status block).
# mc-4asp: consumers read the handoff from origin/main, and a late handoff
# (07-31 landed at 06:18, job ran 06:00) must WAIT rather than publish a blank.
# An empty briefing is never pushed - that is a silent failure on the device.
# mc-p9xd: "no handoff" is now a reported state, not the end of the run.
# ---------------------------------------------------------------------------
OUT="$TMP/morning-briefing-$TODAY.md"
BODY="$TMP/morning-briefing-body-$TODAY.md"
HANDOFF_PATTERN="${RMW_BRIEFING_HANDOFF_PATTERN:-}"
[ -n "$HANDOFF_PATTERN" ] || HANDOFF_PATTERN='{date}-overnight.md'
HANDOFF_REL="${HANDOFF_PATTERN//\{date\}/$TODAY}"
HANDOFF_PRESENT=0

fetch_body() {
  # Prefer the handoff as landed on origin/main; fall back to the working tree
  # (covers offline mornings and same-machine close-outs not yet pushed).
  git -C "$REPO" fetch -q origin main 2>/dev/null || true
  local raw
  raw="$(git -C "$REPO" show "origin/main:$HANDOFF_REL" 2>/dev/null \
         || cat "$REPO/$HANDOFF_REL" 2>/dev/null)"
  if [ -n "${raw//[[:space:]]/}" ]; then HANDOFF_PRESENT=1; else HANDOFF_PRESENT=0; fi
  printf '%s\n' "$raw" \
    | awk '/^## Morning Briefing/{f=1;next} /^## /{if(f)exit} f' > "$BODY"
}

run_briefing() {
  fetch_body
  local try=0
  while ! grep -q '[^[:space:]]' "$BODY" && [ "$try" -lt "$RETRIES" ]; do
    try=$((try+1))
    note "handoff missing or briefing section empty; retry $try/$RETRIES in ${RETRY_DELAY}s"
    sleep "$RETRY_DELAY"
    fetch_body
  done

  if ! grep -q '[^[:space:]]' "$BODY"; then
    # The explicit no-handoff state. Report it; never fabricate a stub.
    if [ "$HANDOFF_PRESENT" = "1" ]; then
      BRIEF_PHRASE="briefing withheld (no briefing in the overnight handoff)"
      note "BRIEFING WITHHELD: handoff exists for $TODAY but its Morning Briefing section is empty"
      bus_note "Morning briefing WITHHELD for $TODAY" \
        "The overnight handoff exists but has no Morning Briefing section, so nothing was pushed. The Decisions document was attempted independently. Log: /tmp/morning-dashboard.log" \
        "briefing-withheld-empty-section"
    else
      BRIEF_PHRASE="briefing withheld (no overnight handoff to process)"
      note "BRIEFING WITHHELD: no overnight handoff for $TODAY after $RETRIES retries"
      bus_note "Morning briefing WITHHELD for $TODAY" \
        "No overnight handoff exists for $TODAY, so no briefing was pushed. The Decisions document was attempted independently. Log: /tmp/morning-dashboard.log" \
        "briefing-withheld-no-handoff"
    fi
    bump "$HARD"
    return 0
  fi

  if ! python3 "$GENERATE_PY" --mode briefing --briefing-file "$BODY" ${CAL_ARG[@]+"${CAL_ARG[@]}"} -o "$OUT"; then
    BRIEF_PHRASE="briefing withheld (the document could not be built)"
    note "briefing: GENERATE FAILED"
    bump "$HARD"
    bus_note "Morning briefing generation FAILED for $TODAY" \
      "generate-dashboard.py failed during the 06:00 job. Log: /tmp/morning-dashboard.log" \
      "briefing-generate-failed"
    return 0
  fi

  if ! outbound_on; then
    BRIEF_PHRASE="briefing withheld (outbound was disabled)"
    note "briefing: built but not pushed (outbound disabled)"
    return 0
  fi

  # Same-day rerun (or the close-out early copy): harvest pen marks, then replace.
  local base rc w pdf
  local -a replace=()
  base="morning-briefing-$TODAY"
  if "$RMAPI" ls "$DEST" 2>/dev/null | awk -F'\t' '{print $2}' | grep -Fxq "$base"; then
    w=$(mktemp -d)
    (cd "$w" && "$RMAPI" geta "$DEST/$base" >/dev/null 2>&1) || true
    # shellcheck disable=SC2012  # carried from the original; these names are ours
    pdf=$(ls -t "$w"/*-annotations.pdf 2>/dev/null | head -1)
    if [ -n "$pdf" ]; then
      mkdir -p "$BRIEF_ARCHIVE"
      mv "$pdf" "$BRIEF_ARCHIVE/$base-annotations.pdf"
    fi
    rm -rf "$w"
    replace=(--replace)
  fi

  outbound "briefing push" "$PUSH_BRIEF" "$OUT" --title "Morning Briefing $TODAY" --dest "$DEST" ${replace[@]+"${replace[@]}"}
  rc=$?
  if [ "$rc" -eq 0 ]; then
    BRIEF_PHRASE="briefing delivered"
    note "briefing: delivered"
  elif [ "$rc" -eq "$SUPPRESSED" ]; then
    BRIEF_PHRASE="briefing withheld (outbound was disabled)"
    note "briefing: not pushed (outbound disabled)"
  else
    BRIEF_PHRASE="briefing withheld (delivery could not be verified)"
    note "briefing: PUSH FAILED"
    bump "$HARD"
    bus_note "Morning briefing push FAILED for $TODAY" \
      "push-brief.sh failed during the 06:00 job. The Decisions document was attempted independently. Log: /tmp/morning-dashboard.log" \
      "briefing-push-failed"
  fi
  return 0
}
run_briefing

# ---------------------------------------------------------------------------
# 5. Rotate superseded editions (moves non-today briefs to Previous Briefs,
# prunes beyond retention, re-baselines annotation watch). Housekeeping only:
# it never changes what the reader is told about this morning's documents. It writes
# to the device, so it goes through the guard like everything else.
# ---------------------------------------------------------------------------
if [ ! -x "$ROTATE" ]; then
  note "rotation: script not executable, skipped"
else
  outbound "brief rotation" "$ROTATE"
  ROT_RC=$?
  [ "$ROT_RC" -eq 0 ] || [ "$ROT_RC" -eq "$SUPPRESSED" ] || note "rotation reported errors (non-fatal)"
fi

# ---------------------------------------------------------------------------
# 6. The outcome line. ONE message to the reader per run, success or failure. Plain
# English, phone-readable, no ticket ids, no paths, no jargon. Earlier uncertain
# sends are surfaced to the agents and never appended to the reader's current line.
# ---------------------------------------------------------------------------
# The reader's format, dictated 2026-09-16. One labelled line per item, plain words,
# and NOTHING about the things that are fine. He asked for three changes to the
# old run-on sentence: name each item on its own line; drop the calendar unless
# it is broken ("I'm not really sure calendar available is useful... If it was
# NOT WORKING that would be useful to know"); drop the tablet write test unless
# it fails ("I don't even know what the 'tablet write test' is so it doesn't
# really mean anything to me"). Failures still surface, successes stay quiet.
phrase_body() {
  # Turn an internal phrase into the body of a labelled line.
  printf '%s' "$1" \
    | sed -e 's/^briefing //' -e 's/^decisions //' \
          -e 's/^withheld (\(.*\))$/not generated (\1)/' \
          -e 's/^withheld$/not generated/' \
          -e 's/^delivered$/delivered/'
}
OUTCOME="Morning Status Check:"
OUTCOME="$OUTCOME"$'\n'"- Morning briefing: $(phrase_body "$BRIEF_PHRASE")"
OUTCOME="$OUTCOME"$'\n'"- Decisions document: $(phrase_body "$DEC_PHRASE")"
# Calendar and the tablet probe are exception-only.
case "$CAL_PHRASE" in
  "calendar available") : ;;
  *) OUTCOME="$OUTCOME"$'\n'"- Calendar: $(printf '%s' "$CAL_PHRASE" | sed 's/^calendar //')" ;;
esac
case "$PROBE_PHRASE" in
  "tablet write test passed") : ;;
  *) OUTCOME="$OUTCOME"$'\n'"- Tablet write check: $(printf '%s' "$PROBE_PHRASE" | sed 's/^tablet write test //')" ;;
esac

echo "OUTCOME LINE: $OUTCOME"

write_outcome_record() {
  local status="$1"
  mkdir -p "$OUTCOME_RECORD_DIR" || return 1
  python3 - "$OUTCOME_RECORD" "$TODAY" "$OUTCOME" "$status" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
outcome_record = {"date": sys.argv[2], "outcome": sys.argv[3], "send_status": sys.argv[4]}
fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(outcome_record, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PY
}

write_pending() {
  local pending_dir
  pending_dir="$(dirname "$PENDING")"
  mkdir -p "$pending_dir" || return 1
  PENDING_PATH="$PENDING" PENDING_DATE="$TODAY" PENDING_OUTCOME="$OUTCOME" python3 - <<'PY'
import os
import pathlib
import tempfile

pending_path = pathlib.Path(os.environ["PENDING_PATH"])
fd, temporary_path = tempfile.mkstemp(prefix=pending_path.name + ".", dir=pending_path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as pending_file:
        pending_file.write(os.environ["PENDING_DATE"] + "\t" + os.environ["PENDING_OUTCOME"] + "\n")
        pending_file.flush()
        os.fsync(pending_file.fileno())
    os.replace(temporary_path, pending_path)
finally:
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
PY
}

if ! outbound_on; then
  outbound "imessage to the reader" "$IMESSAGE_ALERT" "$OUTCOME"
  note "outcome: not sent (outbound disabled); no delivery record was created"
else
  if ! write_outcome_record prepared; then
    note "outcome: could not write the daily outcome record"
    bump "$SOFT"
    bus_note "Morning outcome record could NOT be written ($TODAY)" \
      "The morning run reached its final outcome but could not create the record used by the independent 07:00 check." \
      "morning-outcome-record-failed"
  fi
  if ! write_pending; then
    note "outcome: could not write the pre-send marker"
    bump "$SOFT"
    bus_note "Morning outcome pre-send marker could NOT be written ($TODAY)" \
      "The morning run could not create its write-ahead marker. The message attempt will continue, but a crash could make its result ambiguous." \
      "morning-outcome-pending-failed"
  fi
  if [ ! -x "$IMESSAGE_ALERT" ]; then
    note "outcome: SEND FAILED - $IMESSAGE_ALERT is not executable"
    write_outcome_record failed || true
    bump "$SOFT"
    bus_note "Morning outcome message could NOT be sent ($TODAY)" \
      "The iMessage helper was missing, so the reader was told nothing this morning. Outcome was: $OUTCOME" \
      "morning-outcome-send-failed"
  else
    outbound "imessage to the reader" "$IMESSAGE_ALERT" "$OUTCOME"
    SEND_RC=$?
    if [ "$SEND_RC" -eq 0 ]; then
      note "outcome: sent to the reader by iMessage"
      if write_outcome_record sent; then
        rm -f "$PENDING"
      else
        note "outcome: sent, but the sent record could not be committed; marker retained"
        bump "$SOFT"
        bus_note "Morning outcome delivery record is uncertain ($TODAY)" \
          "The iMessage helper accepted the one-line outcome, but the sent record could not be committed. Check the direct chat before retrying anything." \
          "morning-outcome-sent-record-failed"
      fi
    else
      note "outcome: SEND FAILED - leaving the write-ahead marker for agent review"
      write_outcome_record failed || true
      bump "$SOFT"
      bus_note "Morning outcome message could NOT be sent ($TODAY)" \
        "The iMessage bridge did not accept the morning outcome line, so the reader was told nothing this morning. The pending marker is for agent review only. Outcome was: $OUTCOME" \
        "morning-outcome-send-failed"
    fi
  fi
fi

# A run that was NOT a deliberate rehearsal but delivered nothing is a
# misconfiguration, and must not look like a clean morning.
if ! outbound_on && [ "$DRY_RUN" != "1" ]; then
  note "OUTBOUND DISABLED: $OUTBOUND_SUPPRESSED_COUNT outbound action(s) were suppressed. Nothing was delivered."
  bump "$SOFT"
fi

echo "=== done $(date '+%F %T') exit=$WORST suppressed=$OUTBOUND_SUPPRESSED_COUNT ==="
exit "$WORST"
