#!/usr/bin/env bash
# Binding test for push-brief.sh's decisions guard (mc-ttiyq).
#
# The reader 2026-09-09: an answered decisions document is filed, never removed from
# the device. push-brief.sh --replace does geta -> rm -> put, so for a
# decisions-* basename it must refuse BEFORE any rendering or device call.
# Only rmapi and Chrome are substituted: rmapi is a logger that records every
# verb it is asked for, Chrome is /usr/bin/true. If the guard is removed, the
# script reaches pandoc and the rmapi logger, and this suite fails.
#
# Usage: remarkable/test-push-brief-guard.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/push-brief.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
ok() { if "$@"; then echo "  [ ok ] $*"; else echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

cat > "$WORK/rmapi" <<'EOF'
#!/usr/bin/env bash
printf '%s %s\n' "$1" "${2:-}" >> "$CALL_LOG"
case "$1" in
  ls) printf '[f]\t%s\n' "decisions-2026-01-01" ;;
esac
exit 0
EOF
chmod +x "$WORK/rmapi"
: > "$WORK/calls"

printf '# a decisions document\n\ncard one\n' > "$WORK/decisions-2026-01-01.md"
printf '# a brief\n\nbody\n' > "$WORK/gpu2-testing-brief-2026-01-01.md"

echo "a decisions document with --replace is refused before any device call:"
CALL_LOG="$WORK/calls" RMAPI="$WORK/rmapi" CHROME=/usr/bin/true \
  "$SCRIPT" "$WORK/decisions-2026-01-01.md" --replace > "$WORK/out" 2> "$WORK/err"
RC=$?
ok test "$RC" -eq 6
ok grep -q "REFUSED: 'decisions-2026-01-01' is a decisions document" "$WORK/err"
ok grep -q "push-decisions.sh" "$WORK/err"
ok test "$(grep -cE '^(get|geta|rm|put|ls|mkdir) ' "$WORK/calls")" -eq 0

echo "a decisions document WITHOUT --replace is not refused by the guard (the existing same-name abort still applies later):"
: > "$WORK/calls"
CALL_LOG="$WORK/calls" RMAPI="$WORK/rmapi" CHROME=/usr/bin/true \
  "$SCRIPT" "$WORK/decisions-2026-01-01.md" > "$WORK/out2" 2> "$WORK/err2"
ok test "$(grep -c "REFUSED" "$WORK/err2")" -eq 0

echo "a non-decisions brief with --replace passes the guard:"
: > "$WORK/calls"
CALL_LOG="$WORK/calls" RMAPI="$WORK/rmapi" CHROME=/usr/bin/true \
  "$SCRIPT" "$WORK/gpu2-testing-brief-2026-01-01.md" --replace > "$WORK/out3" 2> "$WORK/err3"
ok test "$(grep -c "REFUSED" "$WORK/err3")" -eq 0

echo
if [ "$FAIL" = 0 ]; then
  echo "PASS: push-brief.sh refuses to replace a decisions document before touching the device"
  exit 0
fi
echo "FAIL: $FAIL case(s) regressed"
exit 1
