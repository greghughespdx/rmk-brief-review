#!/usr/bin/env bash
# mc-rsh8: the stage-1 call-count reductions in check-annotations.sh.
#
# Everything here runs against a fake rmapi that COUNTS ITS CALLS, because the
# whole point of the change is the call count. A test that only checked the
# output would pass just as happily on the 31-call version.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/check-annotations.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
check() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }
ok() { echo "[ok] $*"; }

# Two live docs in /Briefs, three archived in /Briefs/Previous Briefs,
# one live doc in /Reviews. Live = 3, archived = 3, total 6.
cat > "$WORK/rmapi" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
json=0
if [ "$1" = "-json" ]; then json=1; shift; fi
list_briefs='today-brief
decisions-2026-08-26'
list_prev='old-1
old-2
old-3'
list_reviews='review-a'
case "$1" in
  find)
    case "$2" in
      /Briefs)
        printf '[d]\t/Briefs/Previous Briefs\n'
        for n in $list_briefs; do printf '[f]\t/Briefs/%s\n' "$n"; done
        for n in $list_prev; do printf '[f]\t/Briefs/Previous Briefs/%s\n' "$n"; done ;;
      /Reviews) for n in $list_reviews; do printf '[f]\t/Reviews/%s\n' "$n"; done ;;
    esac ;;
  ls)
    [ "$json" = 1 ] || { echo "plain ls not used by stage 1" >&2; exit 9; }
    case "$2" in
      /Briefs) names="$list_briefs"; extra='{"id":"d1","name":"Previous Briefs","type":"CollectionType","version":1,"modifiedClient":"","currentPage":0,"starred":false,"parent":"","tags":[]}' ;;
      "/Briefs/Previous Briefs") names="$list_prev"; extra="" ;;
      /Reviews) names="$list_reviews"; extra="" ;;
      *) names=""; extra="" ;;
    esac
    printf '['
    sep=""
    [ -n "$extra" ] && { printf '%s' "$extra"; sep=","; }
    for n in $names; do
      tags='[]'
      [ "$n" = "today-brief" ] && tags='["ready"]'
      [ "$n" = "old-1" ] && [ -n "${READY_ARCHIVE:-}" ] && tags='["ready"]'
      printf '%s{"id":"%s","name":"%s","type":"DocumentType","version":1,"modifiedClient":"%s","currentPage":0,"starred":false,"parent":"p","tags":%s}' \
        "$sep" "$n" "$n" "${MOD:-2026-08-26T00:00:00Z}" "$tags"
      sep=","
    done
    printf ']\n' ;;
  stat)
    tags='[]'
    case "$2" in
      */today-brief) tags='["ready"]' ;;
      *"Previous Briefs/old-1") [ -n "${READY_ARCHIVE:-}" ] && tags='["ready"]' ;;
    esac
    printf '{"ID":"x","Name":"n","ModifiedClient":"%s","Tags":%s}\n' "${MOD:-2026-08-26T00:00:00Z}" "$tags" ;;
esac
EOF
chmod +x "$WORK/rmapi"

printf '/Briefs\n/Reviews\n' > "$WORK/trees"
run() { # run <mode> [extra env assignments already exported]
  : > "$WORK/calls"
  HOME="$WORK" CALLS="$WORK/calls" RMAPI="$WORK/rmapi" \
    TAG_FLAGS="$HERE/tag_flags.py" "$SCRIPT" "$@" 2>/dev/null
}
ncalls() { wc -l < "$WORK/calls" | tr -d ' '; }
cp "$WORK/trees" "$WORK/.remarkable-watched-trees"
STATE="$WORK/.remarkable-annotation-state.json"

# --- baseline run: scans everything, including the archive -----------------

rm -f "$STATE" "$WORK/.remarkable-annotation-state.json.archive-stamp"
run --baseline >/dev/null
check test "$(ncalls)" -eq 8            # 2 find + 6 stat
check test "$(wc -l < "$STATE" | tr -d ' ')" -eq 6
check test "$(grep -c 'Previous Briefs' "$STATE")" -eq 3
ok "baseline saw every document (6 rows from 8 calls)"

# --- normal cycle right after: archive is skipped ---------------------------

run >/dev/null
check test "$(ncalls)" -eq 5            # 2 find + 3 stat, NOT 8
if grep -q 'Previous Briefs' "$WORK/calls"; then
  fail "an archived document was stat-polled on a normal cycle"
else
  ok "no archived document was stat-polled on a normal cycle"
fi

# The archived rows must SURVIVE being skipped. This is the whole risk of the
# change: a skipped document that loses its row looks brand new later and can
# fire a false transition.
check test "$(wc -l < "$STATE" | tr -d ' ')" -eq 6
check test "$(grep -c 'Previous Briefs' "$STATE")" -eq 3
before=$(grep 'Previous Briefs/old-1' "$STATE")
run >/dev/null
check test "$(grep 'Previous Briefs/old-1' "$STATE")" = "$before"
ok "skipped archive rows are carried forward byte-for-byte"

# --- the hourly archive scan does come back --------------------------------

echo 0 > "$WORK/.remarkable-annotation-state.json.archive-stamp"   # pretend an hour passed
run >/dev/null
check test "$(ncalls)" -eq 8
ok "archive is rescanned once the interval elapses"

# --- an archived doc gaining ink is DEFERRED, not lost ----------------------
#
# The doc has to carry a ready tag to be capable of firing at all, so this
# scenario gives it one. Without that, an archived doc never fires under any
# scanner and the latency claim would be untestable rather than true.

export READY_ARCHIVE=1
rm -f "$STATE" "$WORK/.remarkable-annotation-state.json.archive-stamp"
run --baseline >/dev/null
check test "$(grep -c "Previous Briefs/old-1" "$STATE")" -eq 1

# Mid-hour: every mtime moves. The live tagged doc fires, the archived one
# must not, because it was not scanned.
echo 9999999999 > "$WORK/.remarkable-annotation-state.json.archive-stamp"
out=$(MOD=2026-08-27T00:00:00Z run)
check test "$out" = "/Briefs/today-brief"
if printf '%s' "$out" | grep -q "Previous Briefs"; then
  fail "an archived document fired mid-hour; it should not have been scanned"
else
  ok "archived ink is not reported mid-hour (the accepted latency cost)"
fi

# Once the hour elapses the archived document IS scanned again, so its new
# state is recorded rather than lost. It still does not fire: check-annotations
# never fires a path under Previous Briefs (the reader's signal that a filed
# note should be seen is moving it back into Briefs). This assertion used to
# expect a firing, which predates that rule and failed against it.
echo 0 > "$WORK/.remarkable-annotation-state.json.archive-stamp"
out=$(MOD=2026-08-27T00:00:00Z run)
if grep "Previous Briefs/old-1" "$STATE" | grep -q "2026-08-27"; then
  ok "the hourly scan records the archived document's new state (deferred, not lost)"
else
  fail "the archived document's state was not refreshed by the hourly scan"
fi
if printf '%s' "$out" | grep -q "Previous Briefs/old-1"; then
  fail "a document under Previous Briefs fired; filed documents never fire"
else
  ok "a document under Previous Briefs does not fire, by design"
fi
unset READY_ARCHIVE

# --- scanner B: -json ls collapses the stat calls away ----------------------

rm -f "$STATE" "$WORK/.remarkable-annotation-state.json.archive-stamp"
export RM_STAGE1_JSON=1
run --baseline >/dev/null
# discovery sweep (2 find) + one -json ls per directory (3)
check test "$(grep -c '^find' "$WORK/calls")" -eq 2
check test "$(grep -c '^-json ls' "$WORK/calls")" -eq 3
check test "$(grep -c '^stat' "$WORK/calls")" -eq 0
check test "$(wc -l < "$STATE" | tr -d ' ')" -eq 6
ok "scanner B produced the same 6 rows with zero stat calls"

run >/dev/null
check test "$(ncalls)" -eq 2            # one -json ls per live tree, nothing else
check test "$(grep -c '^stat' "$WORK/calls")" -eq 0
ok "scanner B steady-state cycle is 2 calls, down from 8"
unset RM_STAGE1_JSON

# --- both scanners must agree, or the flag is not safe to flip --------------

rm -f "$STATE" "$WORK/.remarkable-annotation-state.json.archive-stamp"
run --baseline >/dev/null
sort "$STATE" > "$WORK/state-a"
rm -f "$STATE" "$WORK/.remarkable-annotation-state.json.archive-stamp"
RM_STAGE1_JSON=1 run --baseline >/dev/null
sort "$STATE" > "$WORK/state-b"
if diff -q "$WORK/state-a" "$WORK/state-b" >/dev/null; then
  ok "scanner A and scanner B produce byte-identical state"
else
  fail "scanners disagree; RM_STAGE1_JSON is not safe to enable"
  diff "$WORK/state-a" "$WORK/state-b" | head
fi

# --- a failed listing must never wipe the state ----------------------------

rm -f "$STATE" "$WORK/.remarkable-annotation-state.json.archive-stamp"
run --baseline >/dev/null
rows=$(wc -l < "$STATE" | tr -d ' ')
cat > "$WORK/rmapi" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
exit 1
EOF
chmod +x "$WORK/rmapi"
run --baseline >/dev/null
check test "$(wc -l < "$STATE" | tr -d ' ')" -eq "$rows"
ok "a totally failed scan kept the previous state instead of deleting it"

exit "$FAIL"
