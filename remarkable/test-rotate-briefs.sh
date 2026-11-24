#!/usr/bin/env bash
# Binding tests for rotate-briefs.sh (mc-1nr9d).
#
# Pins the reader's 2026-09-04 words: a decisions document files into Previous
# Briefs only once he has set "Mark-as-Read" on it; without that tag it stays
# in /Briefs regardless of age. Automation must never set or clear the tag
# itself. Substitutes only rmapi (a fake device filesystem under $DEVICE,
# same style as test-replace-decisions-document.sh) and the check-annotations
# re-baseline call (a no-op stub next to a harness copy of the script, so this
# suite never touches the real annotation-watch state file or the real
# reMarkable). The real tag_flags.py parser is exercised via TAG_FLAGS, same
# as test-annotation-tags.sh, so the tag decision itself is not reimplemented
# here.
#
# Usage: remarkable/test-rotate-briefs.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/rotate-briefs.sh"
TAG_FLAGS_SRC="$HERE/tag_flags.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
ok() { if "$@"; then echo "  [ ok ] $*"; else echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

DEVICE="$WORK/device"
mkdir -p "$DEVICE/Briefs" "$WORK/harness"

# Harness: a copy of the real script next to a no-op check-annotations.sh, so
# "$(dirname "$0")/check-annotations.sh" resolves to the stub instead of the
# real watcher and its real state file.
cp "$SCRIPT" "$WORK/harness/rotate-briefs.sh"
chmod +x "$WORK/harness/rotate-briefs.sh"
cat > "$WORK/harness/check-annotations.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$WORK/harness/check-annotations.sh"

# Fake device filesystem: real files under $DEVICE, one optional "<path>.tags"
# sidecar per file holding a JSON tag array (as `rmapi stat`'s Tags field would
# report). geta always fails (harvest is documented non-fatal); this also
# means the real archive directory under thoughts/remarkable-archive is never
# written to by this suite.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/rmapi" <<'EOF'
#!/usr/bin/env bash
cmd="$1"; shift
printf '%s %s\n' "$cmd" "$*" >> "$CALL_LOG"
case "$cmd" in
  mkdir)
    mkdir -p "$DEVICE/${1#/}"
    ;;
  ls)
    dir="$DEVICE/${1#/}"
    [ -d "$dir" ] || exit 0
    for item in "$dir"/*; do
      [ -e "$item" ] || continue
      case "$item" in *.tags) continue ;; esac
      printf '[f]\t%s\n' "$(basename "$item")"
    done
    ;;
  stat)
    target="$DEVICE/${1#/}"
    [ -e "$target" ] || exit 1
    tags="[]"
    [ -f "$target.tags" ] && tags="$(cat "$target.tags")"
    mtime=$(stat -f %m "$target" 2>/dev/null || stat -c %Y "$target" 2>/dev/null)
    printf '{"ModifiedClient":"m%s","Tags":%s}\n' "$mtime" "$tags"
    ;;
  geta)
    exit 1
    ;;
  mv)
    src="$DEVICE/${1#/}"
    dst="$DEVICE/${2#/}"
    [ -e "$src" ] || exit 1
    mkdir -p "$dst"
    mv "$src" "$dst/" || exit 1
    [ -f "$src.tags" ] && mv "$src.tags" "$dst/"
    ;;
  rm)
    target="$DEVICE/${1#/}"
    rm -f "$target" "$target.tags"
    ;;
  *)
    echo "UNEXPECTED VERB: $cmd" >&2
    exit 9
    ;;
esac
EOF
chmod +x "$WORK/bin/rmapi"

seed() { # seed <name> [tags-json]
  : > "$DEVICE/Briefs/$1"
  [ -n "${2:-}" ] && printf '%s' "$2" > "$DEVICE/Briefs/$1.tags"
}

run() {
  rm -rf "$DEVICE/Briefs/Previous Briefs"
  : > "$WORK/calls"
  DEVICE="$DEVICE" CALL_LOG="$WORK/calls" RMAPI="$WORK/bin/rmapi" \
  TAG_FLAGS="$TAG_FLAGS_SRC" \
  "$WORK/harness/rotate-briefs.sh" > "$WORK/out" 2> "$WORK/err"
  RC=$?
}

# One day old: past "today" (so step 1's rotation loop is in scope) but well
# inside the default 7-day retention window (so step 2 does not immediately
# remove it again, which would mask what step 1 alone did).
YDAY="$(date -v-1d +%F 2>/dev/null || date -d '-1 day' +%F)"

echo "a decisions document without Mark-as-Read stays in /Briefs:"
seed "decisions-$YDAY"
run
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/decisions-$YDAY"
ok test ! -e "$DEVICE/Briefs/Previous Briefs/decisions-$YDAY"
ok grep -q "held (no Mark-as-Read yet): decisions-$YDAY" "$WORK/out"

echo "a decisions document WITH Mark-as-Read is filed, and the move is logged:"
rm -f "$DEVICE/Briefs/decisions-$YDAY" "$DEVICE/Briefs/decisions-$YDAY.tags"
seed "decisions-$YDAY" '["Mark-as-Read"]'
run
ok test ! -e "$DEVICE/Briefs/decisions-$YDAY"
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-$YDAY"
ok grep -q "rotating (Mark-as-Read confirmed): decisions-$YDAY" "$WORK/out"
ok grep -q "moved to Previous Briefs: decisions-$YDAY" "$WORK/out"

echo "spelling tolerance: any Mark-as-Read form still files it:"
rm -f "$DEVICE/Briefs/decisions-$YDAY" "$DEVICE/Briefs/decisions-$YDAY.tags"
seed "decisions-$YDAY" '["mark_as_read"]'
run
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-$YDAY"

echo "a non-daily brief without Mark-as-Read stays in /Briefs regardless of age (mc-1gx9o):"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "gpu2-testing-brief-2026-09-03"
run
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/gpu2-testing-brief-2026-09-03"
ok grep -q "held (no Mark-as-Read yet): gpu2-testing-brief-2026-09-03" "$WORK/out"

echo "a non-daily brief tagged with the reader's real spelling, Marked as Read, is filed (mc-1gx9o):"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "a10m-vs-gpu2-2026-09-04" '["Marked as Read"]'
seed "garden-plan-changes-2026-09-04"
run
ok test ! -e "$DEVICE/Briefs/a10m-vs-gpu2-2026-09-04"
ok test -e "$DEVICE/Briefs/Previous Briefs/a10m-vs-gpu2-2026-09-04"
ok grep -q "rotating (Mark-as-Read confirmed): a10m-vs-gpu2-2026-09-04" "$WORK/out"
ok test -e "$DEVICE/Briefs/garden-plan-changes-2026-09-04"

echo "today's decisions document is never rotated, tagged or not:"
rm -rf "$DEVICE/Briefs"
mkdir -p "$DEVICE/Briefs"
TODAY="$(date +%F)"
seed "decisions-$TODAY" '["Mark-as-Read"]'
run
ok test -e "$DEVICE/Briefs/decisions-$TODAY"
ok test ! -e "$DEVICE/Briefs/Previous Briefs/decisions-$TODAY"

echo "each daily family (morning-briefing, morning-dashboard, today) rotates unconditionally when not today's:"
for family in morning-briefing morning-dashboard today; do
  rm -rf "$DEVICE/Briefs"
  mkdir -p "$DEVICE/Briefs"
  seed "$family-$YDAY"
  run
  ok test ! -e "$DEVICE/Briefs/$family-$YDAY"
  ok test -e "$DEVICE/Briefs/Previous Briefs/$family-$YDAY"
  ok grep -q "rotating: $family-$YDAY" "$WORK/out"
done

echo "today's daily documents never rotate (mc-1gx9o: today-TODAY was missing from the skip list):"
for family in morning-briefing morning-dashboard today; do
  rm -rf "$DEVICE/Briefs"
  mkdir -p "$DEVICE/Briefs"
  seed "$family-$TODAY"
  run
  ok test -e "$DEVICE/Briefs/$family-$TODAY"
  ok test ! -e "$DEVICE/Briefs/Previous Briefs/$family-$TODAY"
done

echo "retention: an over-7-day decisions document in Previous Briefs is never pruned; an over-7-day daily brief is (mc-ttiyq):"
OLD="$(date -v-30d +%F 2>/dev/null || date -d '-30 days' +%F)"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs/Previous Briefs"
: > "$DEVICE/Briefs/Previous Briefs/decisions-$OLD"
: > "$DEVICE/Briefs/Previous Briefs/morning-briefing-$OLD"
: > "$WORK/calls"
DEVICE="$DEVICE" CALL_LOG="$WORK/calls" RMAPI="$WORK/bin/rmapi" TAG_FLAGS="$TAG_FLAGS_SRC" \
  "$WORK/harness/rotate-briefs.sh" > "$WORK/out" 2> "$WORK/err"
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-$OLD"
ok test ! -e "$DEVICE/Briefs/Previous Briefs/morning-briefing-$OLD"
ok grep -q "pruning (older than 7d): morning-briefing-$OLD" "$WORK/out"
ok test "$(grep -c "pruning (older than 7d): decisions-$OLD" "$WORK/out")" -eq 0
# Harvest ordering pinned from the call log: geta for the old brief is attempted
# BEFORE its rm, and no rm is ever issued for the decisions document.
GETA_LINE=$(grep -n "^geta .*morning-briefing-$OLD" "$WORK/calls" | head -1 | cut -d: -f1)
RM_LINE=$(grep -n "^rm .*morning-briefing-$OLD" "$WORK/calls" | head -1 | cut -d: -f1)
ok test -n "$GETA_LINE"
ok test -n "$RM_LINE"
ok test "${GETA_LINE:-0}" -lt "${RM_LINE:-0}"
ok test "$(grep -c "^rm .*decisions-$OLD" "$WORK/calls")" -eq 0

echo "automation never sets or clears a tag itself (no put call, ever):"
ok test "$(grep -c '^put ' "$WORK/calls" 2>/dev/null || true)" -eq 0

echo
if [ "$FAIL" = 0 ]; then
  echo "PASS: rotation files decisions and every non-daily document only on the read receipt, rotates daily families by date, and logs each decision"
  exit 0
fi
echo "FAIL: $FAIL case(s) regressed"
exit 1
