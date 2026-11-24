#!/bin/bash
# morning-status-watch.sh - tell the reader when a morning item he was told was WRONG
# at 06:00 becomes right.
#
# Origin (the reader, 2026-09-16): "the only message that I got on my phone... is
# stale... if I received information that went stale, I was provided fresh
# information." And, correcting the first build: "it should be conditional. If
# there was nothing wrong when the first message was sent, then the worker
# doesn't need to be monitoring... Just send me the updated status for the
# individual thing or things that did change. Not a spam message that has one
# thing that changed in it."
#
# So:
#   - It watches ONLY the items the 06:00 line reported as not delivered. If
#     that line was clean, this exits immediately and never runs again today.
#   - It reports ONLY the items that changed since he was last told, never the
#     whole picture again.
#   - It stops for the day once every watched item is resolved.
#
# Why a watcher rather than a rule telling agents to send a correction: on
# 2026-09-16 the orchestrator fixed both withheld artifacts and told the reader nothing. A rule
# depends on an agent remembering. A timer does not.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RMAPI="${MB_RMAPI:-$DIR/rmapi-retry.sh}"
IMESSAGE_ALERT="${MB_IMESSAGE_ALERT:-$DIR/hooks/notify.sh}"
STATE_DIR="${MB_OUTCOME_RECORD_DIR:-$HOME/.morning-outcomes}"
TODAY="$(date +%F)"
STATE="$STATE_DIR/${TODAY}-watch.json"
LOG="${MB_WATCH_LOG:-/tmp/morning-status-watch.log}"
WINDOW_END_HOUR="${MB_WATCH_END_HOUR:-10}"
MAX_SENDS="${MB_WATCH_MAX_SENDS:-3}"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG"; }
mkdir -p "$STATE_DIR"

HOUR=$(date +%H)
if [ "$((10#$HOUR))" -ge "$((10#$WINDOW_END_HOUR))" ]; then
  log "outside window; exiting"; exit 0
fi

# --- What was wrong at 06:00? Establish the watch list once. -----------------
if [ -f "$STATE" ]; then
  WATCHING=$(python3 -c 'import json,sys;print(",".join(json.load(open(sys.argv[1])).get("watching",[])))' "$STATE" 2>/dev/null)
  SENDS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("sends",0))' "$STATE" 2>/dev/null)
else
  SEED="$STATE_DIR/${TODAY}.json"
  if [ ! -f "$SEED" ]; then
    log "no 06:00 outcome record yet; nothing to watch"; exit 0
  fi
  SEED_LINE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("outcome",""))' "$SEED" 2>/dev/null)
  WATCHING=""
  # Matches the 06:00 wording since 2026-09-16 ("Morning briefing: delivered",
  # "Decisions document: delivered") and the older "briefing delivered".
  # "not delivered" never matches: "not" sits between the colon and the word.
  printf '%s' "$SEED_LINE" | grep -qiE "briefing:? delivered" || WATCHING="briefing"
  if ! printf '%s' "$SEED_LINE" | grep -qiE "decisions( document)?:? delivered"; then
    [ -n "$WATCHING" ] && WATCHING="$WATCHING,decisions" || WATCHING="decisions"
  fi
  SENDS=0
  if [ -z "$WATCHING" ]; then
    log "06:00 line was clean; nothing to watch today"
    python3 - "$STATE" "$TODAY" <<'PY'
import json,os,pathlib,sys,tempfile
path=pathlib.Path(sys.argv[1])
fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=path.parent)
with os.fdopen(fd,"w") as h: json.dump({"date":sys.argv[2],"watching":[],"sends":0,"done":True},h,sort_keys=True); h.write("\n")
os.replace(tmp,path)
PY
    exit 0
  fi
  log "watching: $WATCHING"
fi

if [ -z "$WATCHING" ]; then
  log "watch list empty; done for today"; exit 0
fi

if [ "$SENDS" -ge "$MAX_SENDS" ]; then
  log "send cap reached; silent"; exit 0
fi

# --- Check only the watched items -------------------------------------------
LISTING=""
case ",$WATCHING," in *,briefing,*|*,decisions,*) LISTING="$("$RMAPI" ls "${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}" 2>/dev/null)" || LISTING="";; esac

RESOLVED=""
STILL=""
for item in ${WATCHING//,/ }; do
  case "$item" in
    briefing)  printf '%s' "$LISTING" | grep -q "morning-briefing-${TODAY}" && RESOLVED="$RESOLVED briefing" || STILL="$STILL,briefing" ;;
    decisions) printf '%s' "$LISTING" | grep -q "decisions-${TODAY}"        && RESOLVED="$RESOLVED decisions" || STILL="$STILL,decisions" ;;
  esac
done
STILL="${STILL#,}"
RESOLVED="${RESOLVED# }"

write_state() {
  python3 - "$STATE" "$TODAY" "$1" "$2" <<'PY'
import json,os,pathlib,sys,tempfile
path=pathlib.Path(sys.argv[1])
watching=[w for w in sys.argv[3].split(",") if w]
rec={"date":sys.argv[2],"watching":watching,"sends":int(sys.argv[4]),"done":not watching}
fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=path.parent)
with os.fdopen(fd,"w") as h: json.dump(rec,h,sort_keys=True); h.write("\n")
os.replace(tmp,path)
PY
}

if [ -z "$RESOLVED" ]; then
  log "no change; still waiting on: $STILL"
  write_state "$STILL" "$SENDS"
  exit 0
fi

# --- Message about ONLY what changed -----------------------------------------
name_of() { case "$1" in briefing) printf 'The morning briefing';; decisions) printf 'The decisions document';; esac; }
COUNT=$(printf '%s' "$RESOLVED" | wc -w | tr -d ' ')
if [ "$COUNT" -eq 1 ]; then
  MSG="$(name_of "$RESOLVED") is on your tablet now."
else
  FIRST=$(printf '%s' "$RESOLVED" | cut -d' ' -f1)
  SECOND=$(printf '%s' "$RESOLVED" | cut -d' ' -f2)
  MSG="$(name_of "$FIRST") and $(name_of "$SECOND" | sed 's/^The /the /') are on your tablet now."
fi

if "$IMESSAGE_ALERT" "$MSG"; then
  SENDS=$((SENDS + 1))
  log "sent: $MSG (still waiting on: ${STILL:-nothing})"
  write_state "$STILL" "$SENDS"
  exit 0
fi

log "send FAILED; watch list unchanged, will retry next tick"
exit 1
