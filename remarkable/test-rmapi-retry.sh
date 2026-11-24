#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$HERE/rmapi-retry.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0

# Never let this suite write to the real central call log (mc-rsh8). Every case
# below points the wrapper at a throwaway file, and the throttle at a throwaway
# directory, so running the tests can neither pollute a week of measurement nor
# wedge a lock the live tools share.
export RMAPI_LOG_FILE="$WORK/calls.log"
export RMAPI_THROTTLE_DIR="$WORK/throttle"

cat > "$WORK/rmapi" <<'EOF'
#!/usr/bin/env bash
count=0
[ ! -f "$COUNT_FILE" ] || count="$(cat "$COUNT_FILE")"
count=$((count + 1))
printf '%s\n' "$count" > "$COUNT_FILE"
case "$MODE" in
  recover)
    if [ "$count" -lt 3 ]; then echo "PARTIAL-SHOULD-NOT-ESCAPE"; echo "HTTP 429 Retry-After: 0" >&2; exit 1; fi
    echo "visible"
    ;;
  exhaust) echo "HTTP 429 too many requests" >&2; exit 1 ;;
  semantic) echo "document not found" >&2; exit 7 ;;
  filename429) echo "document-429.pdf not found" >&2; exit 8 ;;
esac
EOF
chmod +x "$WORK/rmapi"

check() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }
ok() { echo "[ok] $*"; }

# --- retry behaviour (pre-existing contract) --------------------------------

COUNT_FILE="$WORK/count" MODE=recover RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" ls /Briefs > "$WORK/out" 2> "$WORK/err"
rc=$?
check test "$rc" -eq 0
check test "$(cat "$WORK/count")" -eq 3
check grep -Fxq visible "$WORK/out"
if grep -q PARTIAL-SHOULD-NOT-ESCAPE "$WORK/out"; then
  fail "partial output from a failed attempt escaped"
else
  ok "failed-attempt output stayed buffered"
fi

# --- central call log: shape (mc-rsh8 deliverable D) ------------------------
#
# The run above was two 429s then a success, so the log must hold exactly three
# lines - one per ATTEMPT, because each attempt is a real process and real cloud
# traffic. A per-invocation line would undercount the calls a week of data is
# supposed to measure.

check test -f "$RMAPI_LOG_FILE"
check test "$(wc -l < "$RMAPI_LOG_FILE" | tr -d ' ')" -eq 3

check test "$(awk -F'\t' '{print NF}' "$RMAPI_LOG_FILE" | sort -u)" = "9"

# field 1 timestamp: ISO-8601 local time with a numeric offset
if awk -F'\t' '$1 !~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[-+][0-9]{4}$/ {bad=1} END {exit bad}' "$RMAPI_LOG_FILE"; then
  ok "every log line carries an ISO-8601 timestamp with offset"
else
  fail "a log line has a malformed timestamp"
fi

# field 2 caller: detected from the parent process, which is this test script.
# Detection reads ps(1); a sandbox that forbids ps (the offline test runner does)
# makes the wrapper report "unknown" by design, so the check needs ps to work.
if ps -o args= -p "$$" >/dev/null 2>&1; then
  check test "$(awk -F'\t' 'NR==1 {print $2}' "$RMAPI_LOG_FILE")" = "test-rmapi-retry.sh"
else
  check test "$(awk -F'\t' 'NR==1 {print $2}' "$RMAPI_LOG_FILE")" = "unknown"
fi
# field 3 verb, field 4 target
check test "$(awk -F'\t' 'NR==1 {print $3}' "$RMAPI_LOG_FILE")" = "ls"
check test "$(awk -F'\t' 'NR==1 {print $4}' "$RMAPI_LOG_FILE")" = "/Briefs"
# field 5 duration: an integer count of milliseconds
if awk -F'\t' '$5 !~ /^[0-9]+$/ {bad=1} END {exit bad}' "$RMAPI_LOG_FILE"; then
  ok "every log line carries an integer duration"
else
  fail "a log line has a non-integer duration"
fi
# field 6 exit codes, field 7 attempt numbers, in order
check test "$(awk -F'\t' '{printf "%s,", $6}' "$RMAPI_LOG_FILE")" = "1,1,0,"
check test "$(awk -F'\t' '{printf "%s,", $7}' "$RMAPI_LOG_FILE")" = "1,2,3,"

# --- central call log: a 429 is recorded as such ----------------------------
#
# This is the assertion the whole week of measurement rests on: if rl were not
# set on a rate-limited attempt, the log could not answer "when did we get 429s".

check test "$(awk -F'\t' '{printf "%s,", $8}' "$RMAPI_LOG_FILE")" = "y,y,n,"
check test "$(awk -F'\t' '$8=="y"' "$RMAPI_LOG_FILE" | wc -l | tr -d ' ')" -eq 2
# field 9 Retry-After: parsed from the 429 stderr, "-" when the header is absent
check test "$(awk -F'\t' '{printf "%s,", $9}' "$RMAPI_LOG_FILE")" = "0,0,-,"

# --- non-rate-limit failures are logged, and logged as NOT rate limited -----

: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" get /missing > /dev/null 2> "$WORK/semantic-err"
rc=$?
check test "$rc" -eq 7
check test "$(cat "$WORK/count")" -eq 1
check test "$(wc -l < "$RMAPI_LOG_FILE" | tr -d ' ')" -eq 1
check test "$(awk -F'\t' '{print $3"|"$4"|"$6"|"$8"|"$9}' "$RMAPI_LOG_FILE")" = "get|/missing|7|n|-"

# A 429 that is only ever part of a FILENAME must not be logged as rate limited,
# for the same reason it must not trigger a retry.
: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=filename429 RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" get /document-429.pdf > /dev/null 2> "$WORK/filename-err"
rc=$?
check test "$rc" -eq 8
check test "$(cat "$WORK/count")" -eq 1
check test "$(awk -F'\t' '{print $8}' "$RMAPI_LOG_FILE")" = "n"

# --- exhausting the retry budget still logs every attempt -------------------

: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=exhaust RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" put file /Briefs > /dev/null 2> "$WORK/exhaust-err"
rc=$?
check test "$rc" -eq 1
check test "$(cat "$WORK/count")" -eq 4
check test "$(grep -c 'HTTP 429' "$WORK/exhaust-err")" -eq 1
check test "$(wc -l < "$RMAPI_LOG_FILE" | tr -d ' ')" -eq 4
check test "$(awk -F'\t' '$8=="y"' "$RMAPI_LOG_FILE" | wc -l | tr -d ' ')" -eq 4
check test "$(awk -F'\t' '{print $4}' "$RMAPI_LOG_FILE" | sort -u)" = "file /Briefs"

# --- caller override and field hygiene --------------------------------------

: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_CALLER=morning-dashboard.sh \
  RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" stat "/Briefs/a	b" > /dev/null 2>&1
check test "$(awk -F'\t' '{print $2}' "$RMAPI_LOG_FILE")" = "morning-dashboard.sh"
# A tab inside an argument must not become a field separator: still 9 fields.
check test "$(awk -F'\t' '{print NF}' "$RMAPI_LOG_FILE")" = "9"
check test "$(awk -F'\t' '{print $4}' "$RMAPI_LOG_FILE")" = "/Briefs/a b"

# --- global flags before the verb (rmapi -json ls /Briefs) ------------------
#
# rmapi takes its own flags before the command. If the log recorded "-json" as
# the verb, every JSON call would collapse into one bucket and the by-verb
# analysis the log exists for would be worthless.

: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" -json ls /Briefs > /dev/null 2>&1
check test "$(awk -F'\t' '{print $3}' "$RMAPI_LOG_FILE")" = "ls"
check test "$(awk -F'\t' '{print $4}' "$RMAPI_LOG_FILE")" = "/Briefs"

: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" -json -v find /Reviews > /dev/null 2>&1
check test "$(awk -F'\t' '{print $3}' "$RMAPI_LOG_FILE")" = "find"
check test "$(awk -F'\t' '{print $4}' "$RMAPI_LOG_FILE")" = "/Reviews"

# A flags-only invocation must not crash or invent a verb.
: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" -version > /dev/null 2>&1
check test "$(awk -F'\t' '{print NF}' "$RMAPI_LOG_FILE")" = "9"
check test "$(awk -F'\t' '{print $4}' "$RMAPI_LOG_FILE")" = "-"

# --- logging can be turned off ----------------------------------------------

: > "$RMAPI_LOG_FILE"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_LOG=0 RMAPI_REAL="$WORK/rmapi" \
  RMAPI_RETRY_DELAYS="0 0 0" "$WRAPPER" ls /Briefs > /dev/null 2>&1
check test "$(wc -c < "$RMAPI_LOG_FILE" | tr -d ' ')" -eq 0

# --- an unwritable log must never break the rmapi call ----------------------

rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=recover RMAPI_LOG_FILE=/dev/full/nope/calls.log \
  RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" ls /Briefs > "$WORK/out2" 2>/dev/null
rc=$?
check test "$rc" -eq 0
check grep -Fxq visible "$WORK/out2"

# --- shared throttle: OFF by default ----------------------------------------

rm -rf "$RMAPI_THROTTLE_DIR"
rm -f "$WORK/count"
COUNT_FILE="$WORK/count" MODE=semantic RMAPI_REAL="$WORK/rmapi" RMAPI_RETRY_DELAYS="0 0 0" \
  "$WRAPPER" ls /Briefs > /dev/null 2>&1
if [ -e "$RMAPI_THROTTLE_DIR" ]; then
  fail "throttle created state while disabled - it must default to off"
else
  ok "throttle default is off and touches nothing"
fi

# --- shared throttle: concurrent callers serialize --------------------------
#
# Two wrapper invocations race. The fake binary brackets its own run with start
# and end markers, so an unthrottled pair interleaves (start start end end) and a
# throttled pair does not (start end start end). This is the property the reader asked
# for: calls queue instead of racing.

cat > "$WORK/slow-rmapi" <<'EOF'
#!/usr/bin/env bash
echo "start-$$" >> "$MARKER"
sleep 1
echo "end-$$" >> "$MARKER"
EOF
chmod +x "$WORK/slow-rmapi"

: > "$WORK/markers-off"
MARKER="$WORK/markers-off" RMAPI_REAL="$WORK/slow-rmapi" "$WRAPPER" ls /Briefs >/dev/null 2>&1 &
MARKER="$WORK/markers-off" RMAPI_REAL="$WORK/slow-rmapi" "$WRAPPER" ls /Reviews >/dev/null 2>&1 &
wait
if [ "$(sed -n '1p;2p' "$WORK/markers-off" | grep -c start)" -eq 2 ]; then
  ok "control: unthrottled callers really do overlap"
else
  fail "control case did not overlap, so the throttle test below proves nothing"
fi

rm -rf "$RMAPI_THROTTLE_DIR"
: > "$WORK/markers-on"
MARKER="$WORK/markers-on" RMAPI_THROTTLE=1 RMAPI_REAL="$WORK/slow-rmapi" \
  "$WRAPPER" ls /Briefs >/dev/null 2>&1 &
MARKER="$WORK/markers-on" RMAPI_THROTTLE=1 RMAPI_REAL="$WORK/slow-rmapi" \
  "$WRAPPER" ls /Reviews >/dev/null 2>&1 &
wait
seq_on=$(sed 's/-[0-9]*$//' "$WORK/markers-on" | paste -sd, -)
check test "$seq_on" = "start,end,start,end"

# --- shared throttle: minimum spacing between calls -------------------------

cat > "$WORK/fast-rmapi" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$WORK/fast-rmapi"

rm -rf "$RMAPI_THROTTLE_DIR"
t_start=$(perl -MTime::HiRes=time -e 'printf "%d", time()*1000')
for _ in 1 2 3; do
  RMAPI_THROTTLE=1 RMAPI_THROTTLE_MIN_INTERVAL_MS=700 RMAPI_REAL="$WORK/fast-rmapi" \
    "$WRAPPER" ls /Briefs >/dev/null 2>&1
done
t_end=$(perl -MTime::HiRes=time -e 'printf "%d", time()*1000')
elapsed=$((t_end - t_start))
# Three calls with 700ms minimum spacing: the 2nd and 3rd each wait, so at least
# 1400ms total. Asserting the floor only - an upper bound would be flaky.
if [ "$elapsed" -ge 1400 ]; then
  ok "minimum spacing enforced across invocations (${elapsed}ms for 3 calls)"
else
  fail "minimum spacing not enforced (${elapsed}ms for 3 calls, expected >=1400)"
fi

# --- shared throttle: a stale lock is reclaimed, never wedges the rail -------

rm -rf "$RMAPI_THROTTLE_DIR"
mkdir -p "$RMAPI_THROTTLE_DIR/lock"
# Backdate the abandoned lock well past the staleness threshold.
touch -t 202001010000 "$RMAPI_THROTTLE_DIR/lock"
RMAPI_THROTTLE=1 RMAPI_THROTTLE_STALE=60 RMAPI_THROTTLE_TIMEOUT=5 \
  RMAPI_REAL="$WORK/fast-rmapi" "$WRAPPER" ls /Briefs >/dev/null 2>&1
check test "$?" -eq 0

# A lock held by a live holder must not be reclaimed early; the waiter gives up
# and proceeds rather than dropping the call on the floor.
rm -rf "$RMAPI_THROTTLE_DIR"
mkdir -p "$RMAPI_THROTTLE_DIR/lock"
RMAPI_THROTTLE=1 RMAPI_THROTTLE_STALE=3600 RMAPI_THROTTLE_TIMEOUT=2 \
  RMAPI_REAL="$WORK/fast-rmapi" "$WRAPPER" ls /Briefs >/dev/null 2>"$WORK/timeout-err"
rc=$?
check test "$rc" -eq 0
check grep -q "throttle wait exceeded" "$WORK/timeout-err"

exit "$FAIL"
