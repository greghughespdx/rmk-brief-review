#!/usr/bin/env bash
# Run one rmapi command with bounded retry for cloud rate limiting.
#
# Every rmapi call made by the morning delivery path receives this wrapper as
# RMAPI. The real binary is supplied as RMAPI_REAL. Non-rate-limit failures are
# returned immediately. HTTP 429 failures honor Retry-After when rmapi exposes
# it; otherwise they use the configured bounded delay sequence.
#
# mc-rsh8 (2026-08-26): this wrapper is the single choke point every reMarkable
# tool now routes through, so it is also the only place that can answer "what is
# the real 429 window" empirically. Two additions:
#
#   1. CENTRAL CALL LOG (on by default). One tab-separated line per rmapi
#      ATTEMPT - not per wrapper invocation - because each attempt is a real
#      process and real cloud traffic. Counting lines therefore counts actual
#      calls, and filtering rl=y gives the timestamped 429 events a week of data
#      needs to locate the window.
#
#      Path:     ~/Library/Logs/rmapi-calls.log   (override: RMAPI_LOG_FILE)
#      Disable:  RMAPI_LOG=0
#      Format:   ts  caller  verb  target  duration_ms  exit  attempt  rl  retry_after
#        ts           ISO-8601 local time with offset, taken when the attempt STARTED
#        caller       tool that made the call (RMAPI_CALLER, else the parent script)
#        verb         first rmapi argument (ls, stat, find, get, geta, put, mv, rm, mkdir)
#        target       remaining arguments, tabs and newlines squashed to spaces
#        duration_ms  wall time of that one attempt
#        exit         the real rmapi exit code for that attempt
#        attempt      1 for the first try, 2+ for retries after a 429
#        rl           y if this attempt's stderr matched the rate-limit signature
#        retry_after  Retry-After seconds parsed from stderr, or - when absent
#
#      Volume note: at the current 2-minute watcher cadence this file grows by
#      roughly 3 MB a day. It is append-only and is NOT rotated here on purpose -
#      a week of unbroken data is the deliverable. Truncate it deliberately when
#      the study ends.
#
#   2. SHARED THROTTLE (OFF by default, RMAPI_THROTTLE=1 to enable). A per-host
#      mkdir lock plus a minimum spacing between calls, so the tools queue
#      instead of racing. macOS ships no flock(1) and bash 3.2, so the lock is
#      mkdir-based: mkdir on an existing directory fails atomically, which is the
#      portable primitive. Left off until the week of log data says what spacing
#      is actually right - turning it on before then would be guessing.

set -uo pipefail

# The real binary: RMAPI_REAL, else tablet.rmapi from the config (exported by
# rmw-env.sh as RMW_TABLET_RMAPI), else rmapi on PATH. A bare name is resolved
# on PATH so the executable check below sees a real file.
REAL="${RMAPI_REAL:-${RMW_TABLET_RMAPI:-rmapi}}"
case "$REAL" in
  */*) ;;
  *) REAL="$(command -v "$REAL" 2>/dev/null || printf '%s' "$REAL")" ;;
esac
DELAYS="${RMAPI_RETRY_DELAYS:-5 15 30}"
MAX_DELAY="${RMAPI_RETRY_MAX_DELAY:-60}"

LOG_ENABLED="${RMAPI_LOG:-1}"
LOG_FILE="${RMAPI_LOG_FILE:-$HOME/Library/Logs/rmapi-calls.log}"

THROTTLE_ENABLED="${RMAPI_THROTTLE:-0}"
THROTTLE_DIR="${RMAPI_THROTTLE_DIR:-$HOME/.rmapi-throttle}"
THROTTLE_MIN_MS="${RMAPI_THROTTLE_MIN_INTERVAL_MS:-0}"
THROTTLE_TIMEOUT="${RMAPI_THROTTLE_TIMEOUT:-120}"
THROTTLE_STALE="${RMAPI_THROTTLE_STALE:-300}"

RATE_LIMIT_RE='HTTP[^0-9]*429([^0-9]|$)|status([[:space:]_-]*code)?[^0-9]*429([^0-9]|$)|^[[:space:]]*429([^0-9]|$)|too many requests|rate[[:space:]_-]*limit(ed|ing)?'

[ -x "$REAL" ] || {
  echo "rmapi retry: real rmapi is not executable" >&2
  exit 127
}

# Milliseconds since the epoch. bash 5 answers without forking; macOS's system
# bash is 3.2 and has no EPOCHREALTIME, so fall back to perl (always present on
# macOS). Never let a timing failure break the actual rmapi call.
now_ms() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    local e=${EPOCHREALTIME/,/.}
    echo $(( ${e%.*} * 1000 + 10#${e#*.} / 1000 ))
  else
    perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null || echo 0
  fi
}

# Which tool made this call. Tools may name themselves via RMAPI_CALLER; with no
# such hint, read the parent's command line and take the first .sh/.py it names.
# A bash subshell inherits its parent's argv, so a call made inside a pipeline in
# check-annotations.sh still reports check-annotations.sh.
detect_caller() {
  if [ -n "${RMAPI_CALLER:-}" ]; then
    printf '%s' "$RMAPI_CALLER"
    return
  fi
  local args name
  args=$(ps -o args= -p "$PPID" 2>/dev/null)
  for name in $args; do
    case "$name" in
      *.sh|*.py) printf '%s' "${name##*/}"; return ;;
    esac
  done
  printf 'unknown'
}

# Tabs and newlines are the log's field and record separators, so they can never
# appear inside a field. Squash them rather than dropping the value.
scrub() {
  printf '%s' "$*" | tr '\t\n\r' '   '
}

log_attempt() { # log_attempt <ts> <duration_ms> <exit> <attempt> <rl> <retry_after>
  [ "$LOG_ENABLED" = "0" ] && return 0
  local dir="${LOG_FILE%/*}"
  [ "$dir" = "$LOG_FILE" ] || mkdir -p "$dir" 2>/dev/null || return 0
  # One printf of a short line to a file opened O_APPEND is a single atomic
  # write, which is what keeps concurrent tools from interleaving each other's
  # lines without needing a lock on the common path.
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$1" "$CALLER" "$VERB" "$TARGET" "$2" "$3" "$4" "$5" "$6" >> "$LOG_FILE" 2>/dev/null
  return 0
}

LOCK=""
throttle_release() {
  [ -n "$LOCK" ] || return 0
  rmdir "$LOCK" 2>/dev/null
  LOCK=""
}

throttle_acquire() {
  [ "$THROTTLE_ENABLED" = "1" ] || return 0
  mkdir -p "$THROTTLE_DIR" 2>/dev/null || return 0
  local lock="$THROTTLE_DIR/lock" waited=0 age
  while ! mkdir "$lock" 2>/dev/null; do
    # A holder that died without releasing must not wedge the whole rail.
    age=$(perl -e 'print time - (stat($ARGV[0]))[9]' "$lock" 2>/dev/null || echo 0)
    case "$age" in
      ''|*[!0-9]*) age=0 ;;
    esac
    if [ "$age" -gt "$THROTTLE_STALE" ]; then
      rmdir "$lock" 2>/dev/null
      continue
    fi
    if [ "$waited" -ge "$THROTTLE_TIMEOUT" ]; then
      # Never let the throttle itself be the reason a call never happens. The
      # 06:00 delivery matters more than perfect serialization.
      echo "rmapi retry: throttle wait exceeded ${THROTTLE_TIMEOUT}s, proceeding unthrottled" >&2
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  LOCK="$lock"

  # Minimum spacing between consecutive calls across every tool on this host.
  local last now gap
  last=$(cat "$THROTTLE_DIR/last" 2>/dev/null)
  case "$last" in
    ''|*[!0-9]*) last=0 ;;
  esac
  if [ "$THROTTLE_MIN_MS" -gt 0 ] && [ "$last" -gt 0 ]; then
    now=$(now_ms)
    gap=$(( last + THROTTLE_MIN_MS - now ))
    if [ "$gap" -gt 0 ]; then
      sleep "$(perl -e 'printf "%.3f", $ARGV[0]/1000' "$gap" 2>/dev/null || echo 1)"
    fi
  fi
}

work="$(mktemp -d)" || exit 1
trap 'throttle_release; rm -rf "$work"' EXIT

CALLER=$(scrub "$(detect_caller)")

# rmapi takes global flags BEFORE the command (`rmapi -json ls /Briefs`), so
# the verb is the first argument that is not a flag. Without this the log would
# record a verb of "-json" and lump every JSON call into one meaningless
# bucket, which would break the by-verb analysis the whole log exists for.
_vi=1
for _a in "$@"; do
  case "$_a" in
    -*) _vi=$((_vi + 1)) ;;
    *) break ;;
  esac
done
VERB=$(scrub "${!_vi:--}")
if [ "$#" -gt "$_vi" ]; then
  TARGET=$(scrub "${*:$((_vi + 1))}")
else
  TARGET="-"
fi
[ -n "$TARGET" ] || TARGET="-"

throttle_acquire

attempt=1
while :; do
  out="$work/out-$attempt"
  err="$work/err-$attempt"
  started="$(date +%Y-%m-%dT%H:%M:%S%z)"
  t0=$(now_ms)
  "$REAL" "$@" >"$out" 2>"$err"
  rc=$?
  t1=$(now_ms)
  duration=$(( t1 - t0 ))
  [ "$duration" -ge 0 ] || duration=0
  # Stamp when this host last touched the cloud, so the next caller's spacing is
  # measured from the end of a real call rather than from when a lock was taken.
  [ "$THROTTLE_ENABLED" = "1" ] && printf '%s' "$t1" > "$THROTTLE_DIR/last" 2>/dev/null

  retry_after="$(sed -nE 's/.*[Rr]etry-[Aa]fter:[[:space:]]*([0-9]+).*/\1/p' "$err" | head -1)"
  if grep -qiE "$RATE_LIMIT_RE" "$err"; then
    rate_limited=y
  else
    rate_limited=n
  fi
  log_attempt "$started" "$duration" "$rc" "$attempt" "$rate_limited" "${retry_after:--}"

  if [ "$rc" -eq 0 ]; then
    cat "$out"
    cat "$err" >&2
    exit 0
  fi

  if [ "$rate_limited" != "y" ]; then
    cat "$err" >&2
    exit "$rc"
  fi

  delay="$(printf '%s\n' "$DELAYS" | awk -v n="$attempt" '{print $n}')"
  if [ -z "$delay" ]; then
    cat "$err" >&2
    exit "$rc"
  fi

  if [ -n "$retry_after" ]; then
    delay="$retry_after"
  fi
  case "$delay" in
    ''|*[!0-9]*) echo "rmapi retry: invalid retry delay" >&2; exit "$rc" ;;
  esac
  case "$MAX_DELAY" in
    ''|*[!0-9]*) echo "rmapi retry: invalid maximum delay" >&2; exit "$rc" ;;
  esac
  [ "$delay" -le "$MAX_DELAY" ] || delay="$MAX_DELAY"
  echo "rmapi retry: rate limited, retrying in ${delay}s (attempt $attempt)" >&2
  sleep "$delay"
  attempt=$((attempt + 1))
done
