#!/usr/bin/env bash
# Binding tests for unset-ready-tag.sh (mc-1nr9d).
#
# Pins the reader's 2026-09-04 correction: clearing "ready" must never delete or
# re-upload the document. This substitutes only the rmapi binary (a fake
# device filesystem under $DEVICE), the same style as
# test-replace-decisions-document.sh, and asserts directly on the call log
# that rm/put/mv are never invoked.
#
# Usage: remarkable/test-unset-ready-tag.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/unset-ready-tag.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
ok() { if "$@"; then echo "  [ ok ] $*"; else echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

DEVICE="$WORK/device"
mkdir -p "$DEVICE/Briefs" "$WORK/bin"

DEVPATH="/Briefs/decisions-2026-09-04"
STROKE="STROKE-DATA-V1"
EXPECTED_HASH=$(printf '%s' "$STROKE" | shasum -a 256 | cut -d' ' -f1)

# A minimal archive containing one .rm page with known bytes, so the recorded
# ink hash is checkable, not just "some string".
python3 - "$DEVICE/Briefs/decisions-2026-09-04" "$STROKE" <<'PY'
import sys, zipfile
path, stroke = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(path, "w") as z:
    z.writestr("aaaa-bbbb.content", "{}")
    z.writestr("aaaa-bbbb.metadata", "{}")
    z.writestr("aaaa-bbbb/0.rm", stroke)
PY

cat > "$WORK/bin/rmapi" <<'EOF'
#!/usr/bin/env bash
cmd="$1"; shift
printf '%s %s\n' "$cmd" "$*" >> "$CALL_LOG"
case "$cmd" in
  get)
    src="$DEVICE/${1#/}"
    [ -f "$src" ] || exit 1
    cp "$src" "./$(basename "$src").rmdoc"
    ;;
  *)
    echo "UNEXPECTED VERB (must never be called): $cmd" >&2
    exit 9
    ;;
esac
EOF
chmod +x "$WORK/bin/rmapi"
: > "$WORK/calls"

STATE="$WORK/processed.tsv"

run() {
  DEVICE="$DEVICE" CALL_LOG="$WORK/calls" RMAPI="$WORK/bin/rmapi" \
  PROCESSED_STATE="$STATE" UNSET_READY_LOG="$WORK/watch.log" \
  "$SCRIPT" "$DEVPATH" > "$WORK/out" 2> "$WORK/err"
  RC=$?
}

echo "clear-ready keeps the document in place:"
run
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/decisions-2026-09-04"
ok test "$(grep -c '^rm ' "$WORK/calls" 2>/dev/null || true)" -eq 0
ok test "$(grep -c '^put ' "$WORK/calls" 2>/dev/null || true)" -eq 0
ok test "$(grep -c '^mv ' "$WORK/calls" 2>/dev/null || true)" -eq 0
ok grep -qF "$(printf '%s\t%s' "$DEVPATH" "$EXPECTED_HASH")" "$STATE"
ok grep -q 'document left in place, ready tag untouched' "$WORK/out"

echo "a second run replaces the row instead of accumulating history:"
run
ok test "$(wc -l < "$STATE" | tr -d ' ')" -eq 1

echo "an unreadable document fails loudly and still touches nothing:"
rm -f "$DEVICE/Briefs/decisions-2026-09-04"
run
ok test "$RC" -ne 0
ok grep -q 'could not read' "$WORK/out"
ok test "$(grep -c '^rm ' "$WORK/calls" 2>/dev/null || true)" -eq 0
ok test "$(grep -c '^put ' "$WORK/calls" 2>/dev/null || true)" -eq 0

echo
if [ "$FAIL" = 0 ]; then
  echo "PASS: unset-ready-tag never deletes or re-uploads; processed state is recorded on our side"
  exit 0
fi
echo "FAIL: $FAIL case(s) regressed"
exit 1
