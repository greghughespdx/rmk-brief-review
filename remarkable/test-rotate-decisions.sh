#!/usr/bin/env bash
# Tests for rotate-decisions.sh (the reader 2026-09-13 13:30 PDT rule): a decisions-*
# document other than today's is deleted (rmapi mv to /trash) if it has no ink
# and no tag, or filed to "/Briefs/Previous Briefs" if it has either. Today's
# document is never touched. An unreadable document is left in place, not
# guessed at.
#
# Part 1 exercises decide() directly (pure function, no rmapi involved) across
# the full input matrix. Part 2 drives the whole script against a fake rmapi
# fixture filesystem, in the same style as test-rotate-briefs.sh: real files
# under $DEVICE, "<path>.tags" / "<path>.pagetags" JSON sidecars for tags, and
# "<path>.ink" presence for pen strokes. `rmapi rm` is deliberately UNIMPLEMENTED
# in the fake (falls to the "UNEXPECTED VERB" branch) so the suite fails loudly
# if the script is ever changed to hard-delete instead of trashing.
#
# Usage: remarkable/test-rotate-decisions.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/rotate-decisions.sh"
TAG_FLAGS_SRC="$HERE/tag_flags.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
ok() { if "$@"; then echo "  [ ok ] $*"; else echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

echo "=== Part 1: decide() disposition matrix ==="
# shellcheck disable=SC1090
RMAPI=/nonexistent TAG_FLAGS=/nonexistent source "$SCRIPT" >/dev/null 2>&1 || true
# main() is not invoked because $0 != BASH_SOURCE when sourced; decide/fetch
# are now in scope.

ok test "$(decide none n n n n)" = untouched
ok test "$(decide none y n n n)" = touched
ok test "$(decide none n y n n)" = touched
ok test "$(decide none n n y n)" = touched
ok test "$(decide none n n n y)" = touched
ok test "$(decide abc123 n n n n)" = touched
ok test "$(decide '?' n n n n)" = touched

echo
echo "=== Part 2: full script against a fake device ==="

DEVICE="$WORK/device"
mkdir -p "$DEVICE/Briefs/Previous Briefs" "$DEVICE/trash"

mkdir -p "$WORK/bin"
cat > "$WORK/bin/rmapi" <<'FAKE'
#!/usr/bin/env bash
cmd="$1"; shift
printf '%s %s\n' "$cmd" "$*" >> "$CALL_LOG"
case "$cmd" in
  ls)
    dir="$DEVICE/${1#/}"
    [ -d "$dir" ] || exit 0
    # A real device can hold two documents with the SAME visible name; a
    # directory cannot hold two files with one basename. DUP_NAME is the name
    # the listing must report twice (mc-9jd4t).
    [ -n "${DUP_NAME:-}" ] && printf '[f]\t%s\n' "$DUP_NAME"
    for item in "$dir"/*; do
      [ -e "$item" ] || continue
      case "$item" in *.tags|*.pagetags|*.ink) continue ;; esac
      if [ -d "$item" ]; then
        printf '[d]\t%s\n' "$(basename "$item")"
      else
        printf '[f]\t%s\n' "$(basename "$item")"
      fi
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
  get)
    target="$DEVICE/${1#/}"
    [ -e "$target" ] || exit 1
    name="$(basename "$target")"
    work="$(mktemp -d)"
    if [ -f "$target.ink" ]; then
      printf 'INKBYTES' > "$work/page.rm"
    else
      printf '' > "$work/page.rm"
    fi
    pagetags="[]"
    [ -f "$target.pagetags" ] && pagetags="$(cat "$target.pagetags")"
    printf '{"tags":[],"pageTags":%s}' "$pagetags" > "$work/doc.content"
    ( cd "$work" && zip -q "$OLDPWD/$name.rmdoc" page.rm doc.content )
    rm -rf "$work"
    ;;
  mv)
    src="$DEVICE/${1#/}"
    dst="$DEVICE/${2#/}"
    [ -e "$src" ] || exit 1
    mkdir -p "$dst"
    mv "$src" "$dst/" || exit 1
    for suf in .tags .ink .pagetags; do
      [ -f "$src$suf" ] && mv "$src$suf" "$dst/$(basename "$src")$suf"
    done
    true
    ;;
  *)
    echo "UNEXPECTED VERB: $cmd" >&2
    exit 9
    ;;
esac
FAKE
chmod +x "$WORK/bin/rmapi"

seed() { # seed <name> [tags-json] [ink:0|1] [pagetags-json]
  local name="$1" tags="${2:-}" ink="${3:-0}" pagetags="${4:-}"
  : > "$DEVICE/Briefs/$name"
  [ -n "$tags" ] && printf '%s' "$tags" > "$DEVICE/Briefs/$name.tags"
  [ "$ink" = 1 ] && : > "$DEVICE/Briefs/$name.ink"
  [ -n "$pagetags" ] && printf '%s' "$pagetags" > "$DEVICE/Briefs/$name.pagetags"
}

run() {
  rm -rf "$DEVICE/Briefs/Previous Briefs" "$DEVICE/trash"
  mkdir -p "$DEVICE/Briefs/Previous Briefs" "$DEVICE/trash"
  : > "$WORK/calls"
  RMAPI="$WORK/bin/rmapi" TAG_FLAGS="$TAG_FLAGS_SRC" DEVICE="$DEVICE" CALL_LOG="$WORK/calls" \
  DUP_NAME="${DUP_NAME:-}" \
    "$SCRIPT" --date "$1" > "$WORK/out" 2> "$WORK/err"
  RC=$?
}

echo "untouched decisions document (no ink, no tag) is deleted to /trash, not rm:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-10"
seed "decisions-2026-09-13"
run 2026-09-13
ok test "$RC" -eq 0
ok test ! -e "$DEVICE/Briefs/decisions-2026-09-10"
ok test -e "$DEVICE/trash/decisions-2026-09-10"
ok test -e "$DEVICE/Briefs/decisions-2026-09-13"
ok grep -q "deleted (trash): decisions-2026-09-10" "$WORK/out"
ok test "$(grep -c '^rm ' "$WORK/calls" 2>/dev/null || true)" -eq 0
ok grep -q "^mv .*decisions-2026-09-10 /trash" "$WORK/calls"

echo "a document with ink is filed to Previous Briefs, not deleted:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-10" "" 1
seed "decisions-2026-09-13"
run 2026-09-13
ok test ! -e "$DEVICE/Briefs/decisions-2026-09-10"
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-2026-09-10"
ok test ! -e "$DEVICE/trash/decisions-2026-09-10"
ok grep -q "moved to Previous Briefs: decisions-2026-09-10" "$WORK/out"

echo "a document with only a document-level ready tag is filed, not deleted:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-10" '["ready"]'
seed "decisions-2026-09-13"
run 2026-09-13
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-2026-09-10"
ok test ! -e "$DEVICE/trash/decisions-2026-09-10"

echo "a document with only a page-level Marked-as-Read tag is filed, not deleted:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-10" "" 0 '[{"name":"Marked as Read"}]'
seed "decisions-2026-09-13"
run 2026-09-13
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-2026-09-10"
ok test ! -e "$DEVICE/trash/decisions-2026-09-10"

echo "today's own document is never inspected or moved:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-13"
run 2026-09-13
ok test -e "$DEVICE/Briefs/decisions-2026-09-13"
ok test ! -e "$DEVICE/Briefs/Previous Briefs/decisions-2026-09-13"
ok test ! -e "$DEVICE/trash/decisions-2026-09-13"
ok test "$(grep -c '^stat .*decisions-2026-09-13' "$WORK/calls" 2>/dev/null || true)" -eq 0

echo "non-decisions documents in /Briefs are left alone:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
: > "$DEVICE/Briefs/morning-briefing-2026-09-13"
seed "decisions-2026-09-13"
run 2026-09-13
ok test -e "$DEVICE/Briefs/morning-briefing-2026-09-13"

echo "a SECOND document under today's own name is reported, never removed (mc-9jd4t):"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
# Today's document, carrying ink: this is the shape of the 2026-09-13 failure,
# where a delete reported success, did not take, and the reader penned four pages of
# answers onto the stale twin while the fresh one sat beside it.
seed "decisions-2026-09-13" "" 1
DUP_NAME="decisions-2026-09-13" run 2026-09-13
ok test "$RC" -eq 1
ok grep -q "DUPLICATE under today's name, left in place: decisions-2026-09-13" "$WORK/err"
ok grep -q "never deleted" "$WORK/err"
# Left exactly where it was. Not trashed, not filed.
ok test -e "$DEVICE/Briefs/decisions-2026-09-13"
ok test ! -e "$DEVICE/trash/decisions-2026-09-13"
ok test ! -e "$DEVICE/Briefs/Previous Briefs/decisions-2026-09-13"

echo "a second document under today's name with NO ink is still not removed:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-13"
DUP_NAME="decisions-2026-09-13" run 2026-09-13
ok test "$RC" -eq 1
ok grep -q "DUPLICATE under today's name, left in place" "$WORK/err"
# The reason matters: rmapi rm takes a path, and that path matches BOTH
# documents, so removing by name could take the wrong one or both.
ok grep -q "matches both documents" "$WORK/err"
ok test -e "$DEVICE/Briefs/decisions-2026-09-13"
ok test ! -e "$DEVICE/trash/decisions-2026-09-13"

echo "with no duplicate, today's document is still skipped silently:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-13"
DUP_NAME="" run 2026-09-13
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/decisions-2026-09-13"

echo "an unreadable document (stat fails) is left in place, never guessed at:"
rm -rf "$DEVICE/Briefs"; mkdir -p "$DEVICE/Briefs"
seed "decisions-2026-09-13"
mkdir -p "$WORK/ghost"
: > "$WORK/ghost/decisions-2026-09-10"
# Simulate an unreadable doc: list it via ls but stat will fail (no real file
# at that path in $DEVICE), by listing a name with no backing file.
cat > "$WORK/bin/rmapi" <<'FAKE2'
#!/usr/bin/env bash
cmd="$1"; shift
printf '%s %s\n' "$cmd" "$*" >> "$CALL_LOG"
case "$cmd" in
  ls)
    dir="$DEVICE/${1#/}"
    for item in "$dir"/*; do
      [ -e "$item" ] || continue
      case "$item" in *.tags|*.pagetags|*.ink) continue ;; esac
      printf '[f]\t%s\n' "$(basename "$item")"
    done
    printf '[f]\tdecisions-2026-09-10\n'
    ;;
  stat)
    target="$DEVICE/${1#/}"
    [ -e "$target" ] || exit 1
    printf '{"ModifiedClient":"m0","Tags":[]}\n'
    ;;
  *) exit 9 ;;
esac
FAKE2
chmod +x "$WORK/bin/rmapi"
run 2026-09-13
ok test "$RC" -eq 1
ok grep -q "UNREADABLE, left in place: decisions-2026-09-10" "$WORK/err"

echo
if [ "$FAIL" = 0 ]; then
  echo "PASS: rotate-decisions.sh deletes untouched decisions documents to trash, files touched ones to Previous Briefs, and never touches today's document or an unreadable one"
  exit 0
fi
echo "FAIL: $FAIL case(s) regressed"
exit 1
