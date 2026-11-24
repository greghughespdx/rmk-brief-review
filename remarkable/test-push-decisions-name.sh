#!/usr/bin/env bash
# push-decisions.sh --name: deliver a SECOND document for the same day.
#
# mc-9jd4t, 2026-09-13. The reader was marking a 17-card copy of the day's Decisions
# document while a 20-card copy sat beside it, so the three cards missing from
# his copy had to reach him as their own document rather than by replacing the
# one under his pen. Two things have to hold for that to be safe:
#
#   - the named document uses its own PDF, manifest, review receipt, device
#     path and registry entry, so its pen answers resolve on their own;
#   - rotate-decisions.sh, which judges every other decisions-* document in
#     /Briefs by name alone, is NOT run from a supplement. Run from one it
#     would file or trash the very document the supplement completes.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DATE=2026-09-13
NAME="decisions-$DATE-supplement"
ARCHIVE="$WORK/archive"
BIN="$WORK/bin"
FAIL=0

mkdir -p "$BIN" "$ARCHIVE/$DATE/$NAME"
cp "$HERE/push-decisions.sh" "$BIN/push-decisions.sh"

# One realistic card per fixture manifest, in the key shape real manifests use
# (see {archive}/decisions/2026-09-22/*.manifest.json): a
# zero-card manifest makes push-decisions.sh exit 5 ("nothing to ask today",
# 2026-09-16) before it ever reaches the name/replace paths this test asserts.
# Content is fixture-only; push-decisions.sh only reads len(cards).
card() {
  printf '{"ask":"Fixture ask text, not evaluated by push-decisions.sh.","card_id":"card-001","dates":"Raised September 13.","decision_entry_ids":["fixture-entry-%s"],"description":"Fixture description text.","fragments":[],"headline":"Fixture headline","item_id":"fixture-item-%s","layout":{},"page":2,"page_numbers":[2],"printed_question_keys":["fixture_question"],"question_keys":["fixture_question"],"question_set_hash":"fixturehash0000000000000000000000000000000000000000000000000000","read":{},"recommendation":"Fixture recommendation text.","row_key":"fixture_row_%s","system":"Mission control"}' \
    "$1" "$1" "$1"
}

printf 'SUPPLEMENT-PDF\n' > "$ARCHIVE/$DATE/$NAME/$NAME.pdf"
printf '{"cards":[%s],"manifest_hash":"h","page_id":"%s-abc"}\n' "$(card supplement)" "$NAME" \
  > "$ARCHIVE/$DATE/$NAME/$NAME.manifest.json"
printf 'DAILY-PDF\n' > "$ARCHIVE/$DATE/decisions-$DATE.pdf"
printf '{"cards":[%s],"manifest_hash":"h","page_id":"decisions-%s-abc"}\n' "$(card daily)" "$DATE" \
  > "$ARCHIVE/$DATE/decisions-$DATE.manifest.json"

cat > "$BIN/review-receipt.py" <<'PY'
#!/usr/bin/env python3
import os, pathlib, sys
argv = sys.argv[1:]
pathlib.Path(os.environ["CALL_LOG"]).open("a").write("review-receipt " + " ".join(argv) + "\n")
raise SystemExit(0)
PY

cat > "$BIN/replace-decisions-document.sh" <<'SH'
#!/usr/bin/env bash
printf 'replace %s\n' "$*" >> "$CALL_LOG"
SH

cat > "$BIN/rotate-decisions.sh" <<'SH'
#!/usr/bin/env bash
printf 'rotate %s\n' "$*" >> "$CALL_LOG"
SH

# The delivery-time phone-card check (mc-0mtb0) is exercised in
# tests/test_verify_queue_live_cards.py; here it only records which manifest
# it was asked about, and lets the push go ahead.
cat > "$BIN/live-card-delivery-check.py" <<'PY'
import os, pathlib, sys
pathlib.Path(os.environ["CALL_LOG"]).open("a").write("live-check " + " ".join(sys.argv[1:]) + "\n")
PY

cat > "$BIN/fake-python" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$BIN/"*

ok() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

run_push() {
  : > "$WORK/calls"
  CALL_LOG="$WORK/calls" \
  PHONE_CARDS_URL="http://127.0.0.1:9" \
  REMARKABLE_TODAY_ARCHIVE="$ARCHIVE" \
  REMARKABLE_TODAY_REGISTRY="$WORK/registry.json" \
  TODAY_EXTRACTOR_PYTHON="$BIN/fake-python" \
  "$BIN/push-decisions.sh" "$@" > "$WORK/out" 2> "$WORK/err"
  RC=$?
}

# A named document is never rendered here: it has already been reviewed, and
# re-rendering it would invalidate the receipt bound to its bytes.
run_push --date "$DATE" --name "$NAME" --push
ok test "$RC" -eq 2
ok grep -q -- '--name renders nothing' "$WORK/err"

# The supplement: its own files, its own receipt, its own device path, and no
# rotation of the folder it is joining.
run_push --date "$DATE" --name "$NAME" --no-generate --push
ok test "$RC" -eq 0
ok grep -q "replace .*--pdf $ARCHIVE/$DATE/$NAME/$NAME.pdf" "$WORK/calls"
ok grep -q "replace .*--manifest $ARCHIVE/$DATE/$NAME/$NAME.manifest.json" "$WORK/calls"
ok grep -q "replace .*--receipt $ARCHIVE/$DATE/$NAME/review-receipt.json" "$WORK/calls"
ok grep -q "live-check --manifest $ARCHIVE/$DATE/$NAME/$NAME.manifest.json" "$WORK/calls"
ok grep -q "replace .*--device-path /Briefs/$NAME" "$WORK/calls"
ok grep -q "review-receipt .*--receipt $ARCHIVE/$DATE/$NAME/review-receipt.json" "$WORK/calls"
ok test "$(grep -c '^rotate ' "$WORK/calls" || true)" -eq 0
ok grep -q "left in place" "$WORK/out"

# The day's own document is unchanged: same paths as before --name existed,
# and it still clears the folder behind it.
run_push --date "$DATE" --no-generate --push
ok test "$RC" -eq 0
ok grep -q "replace .*--pdf $ARCHIVE/$DATE/decisions-$DATE.pdf" "$WORK/calls"
ok grep -q "replace .*--receipt $ARCHIVE/$DATE/review-receipt.json" "$WORK/calls"
ok grep -q "replace .*--device-path /Briefs/decisions-$DATE" "$WORK/calls"
ok test "$(grep -c '^rotate ' "$WORK/calls" || true)" -eq 1

# Nothing-to-ask coverage (the reader 2026-09-16, mc-n48wb): a manifest with zero
# cards and zero verify-report rows is not a defect, it means there is
# nothing to ask today. push-decisions.sh exits 5 before ever reaching the
# review-receipt/replace/rotate calls this test otherwise asserts, and
# nothing gets pushed.
EMPTY_DATE=2026-09-14
mkdir -p "$ARCHIVE/$EMPTY_DATE"
printf 'EMPTY-PDF\n' > "$ARCHIVE/$EMPTY_DATE/decisions-$EMPTY_DATE.pdf"
printf '{"cards":[],"manifest_hash":"h","page_id":"decisions-%s-abc"}\n' "$EMPTY_DATE" \
  > "$ARCHIVE/$EMPTY_DATE/decisions-$EMPTY_DATE.manifest.json"

run_push --date "$EMPTY_DATE" --no-generate --push
ok test "$RC" -eq 5
ok grep -q "nothing to ask today" "$WORK/err"
ok test ! -s "$WORK/calls"

if [ "$FAIL" -eq 0 ]; then echo "all push-decisions --name checks passed"; else echo "$FAIL check(s) failed"; fi
exit "$FAIL"
