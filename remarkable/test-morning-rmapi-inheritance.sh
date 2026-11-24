#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROTATE="$HERE/rotate-briefs.sh"
WRAPPER="$HERE/rmapi-retry.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0

cat > "$WORK/rmapi" <<'EOF'
#!/usr/bin/env bash
command_name="$1"
shift
key="$(printf '%s-%s' "$command_name" "${1:-root}" | tr '/ ' '__')"
count_file="$FAKE_STATE/$key"
count=0
[ ! -f "$count_file" ] || count="$(cat "$count_file")"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
printf '%s %s\n' "$command_name" "$*" >> "$FAKE_CALLS"
case "$command_name" in
  ls|find)
    if [ "$count" -eq 1 ]; then
      echo "HTTP 429 Retry-After: 0" >&2
      exit 1
    fi
    ;;
  mkdir) ;;
  *) echo "unexpected fake rmapi command: $command_name" >&2; exit 9 ;;
esac
EOF
chmod +x "$WORK/rmapi"
mkdir -p "$WORK/home" "$WORK/state"
: > "$WORK/calls"

HOME="$WORK/home" RMAPI="$WRAPPER" RMAPI_REAL="$WORK/rmapi" \
RMAPI_RETRY_DELAYS="0 0 0" FAKE_STATE="$WORK/state" FAKE_CALLS="$WORK/calls" \
  "$ROTATE" > "$WORK/out" 2> "$WORK/err"
RC=$?

ok() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }
ok test "$RC" -eq 0
ok grep -q '^mkdir /Briefs/Previous Briefs$' "$WORK/calls"
ok test "$(grep -c '^ls /Briefs$' "$WORK/calls")" -eq 3
ok test "$(grep -c '^ls /Briefs/Previous Briefs$' "$WORK/calls")" -eq 3
ok test "$(grep -c '^find /Briefs$' "$WORK/calls")" -eq 2
ok test "$(grep -c '^find /Reviews$' "$WORK/calls")" -eq 2

exit "$FAIL"
