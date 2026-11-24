#!/usr/bin/env bash
# The Decisions document loop, end to end, against a fixture device (mc-j5dr4).
#
# The reader's tablet is his own device, so none of this touches it: the loop runs
# with --dry-run, whose rmapi stub acts on a fixture directory and refuses
# `rm` outright. What is NOT stubbed is every gate: the consume gate, the
# answer gate, and the real review-receipt.py hash check all run for real, so
# a pass here means the gates passed on real bytes.
#
# The fixture carries one penned answer of each kind the reader gives: a plain
# answer, a DEFER, a REMOVE (DISCARD), a penned question, and a card whose
# facts went stale. What each kind does to the FOLLOW-UP DOCUMENT is
# today_page's job and is tested in tests/test_remarkable_today_page.py; what
# they do to the LOOP is tested here.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/decisions-loop.sh"
DATE="2026-08-20"
PAGE="page-$DATE"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ARCHIVE="$WORK/archive"
OUT="$ARCHIVE/$DATE"
PROCESSED="$OUT/processed"
PDF="$OUT/decisions-$DATE.pdf"
MANIFEST="$OUT/decisions-$DATE.manifest.json"
RECEIPT="$OUT/review-receipt.json"
ANSWERS="$OUT/answers-complete.json"
QSTATE="$WORK/question-state.json"
REGISTRY="$WORK/registry.json"
FIXTURE="$WORK/fixture"
DEVICE="$FIXTURE/device"
CALLS="$FIXTURE/calls.log"
PUSH_LOG="$WORK/push-calls.log"
PATH_KEY="/Briefs/decisions-$DATE"
FAIL=0

mkdir -p "$PROCESSED" "$WORK/bin"

ok() {
  if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=1; fi
}
note() { echo; echo "--- $* ---"; }

# --- the fixture: one penned answer of each kind -----------------------------
#
# question-state is what the consumer leaves behind after the reader's ink is
# resolved. These five entries are the five kinds.
write_question_state() {
  cat > "$QSTATE" <<JSON
{"schema_version": 1, "entries": {
  "row-plain::approve":    {"state":"answered","since":"$DATE","source":"$PAGE","detail":"YES"},
  "row-defer::approve":    {"state":"deferred","since":"$DATE","source":"$PAGE","detail":"DEFER"},
  "row-remove::approve":   {"state":"discarded","since":"$DATE","source":"$PAGE","detail":"DISCARD"},
  "row-question::approve": {"state":"needs_clarification","since":"$DATE","source":"$PAGE","detail":"what about the second host?"},
  "row-stale::approve":    {"state":"unanswered","since":"$DATE","source":"$PAGE","detail":"not yet asked"}
}}
JSON
}

# manifest_with <card-id>... : a rendered document holding exactly these cards
manifest_with() {
  local cards="" id
  for id in "$@"; do
    cards="$cards{\"card_id\":\"$id\",\"headline\":\"$id headline\"},"
  done
  printf '{"page_id":"%s","manifest_hash":"mh-%s","cards":[%s]}\n' \
    "$PAGE" "$*" "${cards%,}" > "$MANIFEST"
}

render_document() {  # render_document <pdf-body> <card-id>...
  local body="$1"; shift
  mkdir -p "$OUT"
  printf '%s\n' "$body" > "$PDF"
  manifest_with "$@"
}

write_resolution() {  # write_resolution <page-id>
  printf '{"page_id":"%s","decisions":[]}\n' "$1" > "$PROCESSED/resolution.json"
}

# --- stubs for the two tools the loop shells out to --------------------------
#
# verify-queue.py is stubbed because the real one reaches GitHub and live
# services. The stub does the one thing the loop depends on: it HOLDS the row
# whose facts went stale and writes a queue without it. That is the hard rule
# (the reader 2026-09-13) from the loop's side: the loop renders only from a
# verified queue, never from the raw one.
cat > "$WORK/bin/verify-queue.py" <<'STUB'
import sys, json, pathlib
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
queue = pathlib.Path(args["--out-queue"])
report = pathlib.Path(args["--report"])
rows = ["row-defer", "row-question", "row-stale"]
held = {"row-stale": "the ticket it cites was closed since the card was written"}
kept = [r for r in rows if r not in held]
queue.write_text("\n".join(f"| {r} | ask |" for r in kept) + "\n")
report.write_text(json.dumps({
    "rows": [{"item_id": r, "verified": r not in held,
              "held_reason": held.get(r, "")} for r in rows]
}, indent=2))
print(f"verified {len(kept)} row(s); held {len(held)}")
STUB

# push-decisions.sh is stubbed so nothing renders with reportlab and nothing
# reaches the device. It records every invocation and, on a render (no
# --no-generate), writes the document the loop then counts.
#
# Its page id is derived from the queue it rendered, because the real builder's
# is: today_page.py hashes the content and the source hashes into page_id, so a
# follow-up carrying a different set of cards is a DIFFERENT page. Verified
# against the real today_page.build_manifest for 2026-08-20 - two cards
# decisions-2026-08-20-903e1b3000ef, one card ...-ba75a97b0db0, zero cards
# ...-202c0dc5d9c6. The stub used to reuse $PAGE for every render, which hid a
# real exit-11 failure on file-processed's own intended path (the cross-reviewer review).
cat > "$WORK/bin/push-decisions.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PUSH_LOG"
generate=1
queue=""
for arg in "$@"; do
  [ "$arg" = "--no-generate" ] && generate=0
done
while [ "$#" -gt 0 ]; do
  case "$1" in --queue) queue="$2"; shift 2 ;; *) shift ;; esac
done
if [ "$generate" -eq 1 ]; then
  [ -n "${RENDER_BODY:-}" ] || RENDER_BODY="rendered from $queue"
  printf '%s\n' "$RENDER_BODY" > "$PDF"
  ids=""
  if [ -n "$queue" ] && [ -s "$queue" ]; then
    while IFS= read -r line; do
      id="$(printf '%s' "$line" | awk -F'|' '{print $2}' | tr -d ' ')"
      [ -n "$id" ] && ids="$ids{\"card_id\":\"$id\",\"headline\":\"$id headline\"},"
    done < "$queue"
  fi
  page_render="$PAGE-$( { [ -n "$queue" ] && [ -f "$queue" ] && cat "$queue"; } \
    | shasum | cut -c1-12)"
  printf '{"page_id":"%s","manifest_hash":"mh-%s","cards":[%s]}\n' \
    "$page_render" "$(date +%s%N)" "${ids%,}" > "$MANIFEST"
  [ -n "$ids" ] || exit 5
fi
exit 0
STUB
chmod +x "$WORK/bin/push-decisions.sh"

run_loop() {  # run_loop <command> [extra args...]
  local cmd="$1"; shift
  set +e
  PUSH_LOG="$PUSH_LOG" PDF="$PDF" MANIFEST="$MANIFEST" PAGE="$PAGE" \
  RENDER_BODY="${RENDER_BODY:-}" DRY_MV_FAILS="${DRY_MV_FAILS:-}" \
  DECISIONS_PUSH_DECISIONS="$WORK/bin/push-decisions.sh" \
  DECISIONS_VERIFY_QUEUE="$WORK/bin/verify-queue.py" \
  REMARKABLE_DOC_PRODUCER=orchestrator \
  bash "$SCRIPT" "$cmd" --date "$DATE" --dry-run --fixture-dir "$FIXTURE" \
    --archive-root "$ARCHIVE" --question-state "$QSTATE" --registry "$REGISTRY" \
    "$@" > "$WORK/out" 2> "$WORK/err"
  RC=$?
  set -e
  return 0
}

push_calls() { grep -c . "$PUSH_LOG" 2>/dev/null | head -1 || true; }
manifest_page() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("page_id",""))' "$MANIFEST"; }
resolution_page() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("page_id",""))' "$PROCESSED/resolution.json"; }

# =============================================================================
write_question_state
render_document "PDF-ORIGINAL" row-plain row-defer row-remove row-question row-stale
printf '{"pages":{"%s":{"pushed":true}}}\n' "$PATH_KEY" > "$REGISTRY"
: > "$PUSH_LOG"

note "the loop refuses to re-render a document that has not been consumed"
run_loop render
ok test "$RC" -eq 11
ok grep -q 'NOT CONSUMED' "$WORK/err"
ok test "$(push_calls)" -eq 0

note "a resolution of a DIFFERENT document does not unlock the loop"
write_resolution "page-some-other-day"
run_loop render
ok test "$RC" -eq 11
ok grep -q 'belongs to a different document' "$WORK/err"
ok test "$(push_calls)" -eq 0

note "the penned question holds the loop until a model has answered it"
write_resolution "$PAGE"
run_loop render
ok test "$RC" -eq 12
ok grep -q 'ANSWER GATE' "$WORK/err"
ok grep -q 'clarification:row-question::approve' "$WORK/err"
# Nothing was rendered and nothing was pushed while the question stood open.
ok test "$(push_calls)" -eq 0
ok grep -Fxq PDF-ORIGINAL "$PDF"

note "an answer record for the WRONG page does not satisfy the gate"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"page-other","answered_by":"orchestrator","answered_at":"%s","handled":["clarification:row-question::approve"]}\n' \
  "$DATE" > "$ANSWERS"
run_loop render
ok test "$RC" -eq 12
ok test "$(push_calls)" -eq 0

# ---------------------------------------------------------------------------
# NEGATIVE CONTROL for the cross-reviewer finding 2 (mc-j5dr4): a penned-work check that
# could not be run is never a pass.
#
# "handled": null raised inside the coverage set, the traceback went to stderr,
# and the gate read the empty stdout as "nothing outstanding" - so a document
# with an unanswered penned question rendered and would have gone to the reader.
# Revert the repair and both of these go green at exit 0 with a push recorded.
# ---------------------------------------------------------------------------
note "NEGATIVE CONTROL: an answer record with handled:null fails the gate closed"
pushes_before_malformed="$(push_calls)"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"%s","answered_by":"orchestrator","answered_at":"%s","handled":null}\n' \
  "$PAGE" "$DATE" > "$ANSWERS"
run_loop render
ok test "$RC" -eq 12
ok grep -q 'ANSWER GATE: the penned-work check did not complete' "$WORK/err"
ok grep -q "'handled'" "$WORK/err"
ok test "$(push_calls)" -eq "$pushes_before_malformed"
ok grep -Fxq PDF-ORIGINAL "$PDF"

note "NEGATIVE CONTROL: a truncated answer record fails the gate closed"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"%s","handled":[' \
  "$PAGE" > "$ANSWERS"
run_loop render
ok test "$RC" -eq 12
ok grep -q 'could not be parsed' "$WORK/err"
ok test "$(push_calls)" -eq "$pushes_before_malformed"
ok grep -Fxq PDF-ORIGINAL "$PDF"

note "answered: the loop renders, from the VERIFIED queue only"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"%s","answered_by":"orchestrator","answered_at":"%s","handled":["clarification:row-question::approve"]}\n' \
  "$PAGE" "$DATE" > "$ANSWERS"
RENDER_BODY="PDF-FOLLOWUP" run_loop render
ok test "$RC" -eq 0
ok test "$(push_calls)" -eq 1
ok grep -Fxq PDF-FOLLOWUP "$PDF"
# The stale card was held by verification, so it is not in what was rendered.
ok grep -q 'row-stale' "$OUT/verify-remaining.json"
ok test "$(grep -c row-stale "$OUT/verified-queue-remaining.md" || true)" -eq 0
ok test "$(grep -c row-stale "$MANIFEST" || true)" -eq 0
# The superseded document and its receipt were kept, not overwritten in place.
ok test -n "$(find "$OUT" -maxdepth 1 -name 'superseded-*' -print -quit)"
# The follow-up is a DIFFERENT page from the one the reader answered, the way the
# real builder makes it, and the loop wrote down which page it consumed.
followup_page="$(manifest_page)"
ok test "$followup_page" != "$PAGE"
ok grep -q "\"consumed_page_id\": \"$PAGE\"" "$OUT/loop-state.json"

note "NEGATIVE CONTROL: an unreviewed document is never pushed"
ok test ! -f "$RECEIPT"
run_loop deliver
ok test "$RC" -eq 4
ok grep -q 'REVIEW GATE' "$WORK/err"
# The push tool was not reached at all, and nothing was sent to the device.
ok test "$(push_calls)" -eq 1
ok test "$(grep -c 'no-generate' "$CALLS" 2>/dev/null || true)" -eq 0

note "a receipt from the producer is not an independent review"
python3 "$HERE/review-receipt.py" --archive-root "$ARCHIVE" write --date "$DATE" \
  --reviewer orchestrator --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
  --verdict "row-defer:yes,yes,yes" --verdict "row-question:yes,yes,yes" >/dev/null
run_loop deliver
ok test "$RC" -eq 4
ok grep -q 'who produced the document' "$WORK/err"

note "an independent, hash-bound receipt lets deliver through"
python3 "$HERE/review-receipt.py" --archive-root "$ARCHIVE" write --date "$DATE" \
  --reviewer reviewer-agent --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
  --verdict "row-defer:yes,yes,yes" --verdict "row-question:yes,yes,yes" >/dev/null
run_loop deliver
ok test "$RC" -eq 0
ok grep -q 'DRY RUN: would push' "$WORK/out"
ok grep -q 'no-generate' "$CALLS"

note "NEGATIVE CONTROL: a re-render after the review voids the receipt"
printf 'PDF-RERENDERED-AFTER-REVIEW\n' > "$PDF"
run_loop deliver
ok test "$RC" -eq 4
ok grep -q 'changed since it was reviewed' "$WORK/err"

note "nothing left to ask: the loop stops rather than pushing a blank sheet"
cat > "$WORK/bin/verify-queue.py" <<'STUB'
import sys, json, pathlib
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
pathlib.Path(args["--out-queue"]).write_text("")
pathlib.Path(args["--report"]).write_text(json.dumps({"rows": []}))
STUB
# The remaining cards were all deferred. A previously answered pen note also
# belongs to this same page, not to the empty follow-up.
mkdir -p "$PROCESSED/notes"
touch "$PROCESSED/notes/already-answered.png"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"%s","handled":["notes:already-answered.png"]}\n'   "$followup_page" > "$ANSWERS"
# The follow-up went to the device and came back answered, so the resolution
# on file is now a resolution of the FOLLOW-UP page, not the original.
printf '{"page_id":"%s","decisions":[],"ambiguities":[{"stroke_id":"stroke-large","reason":"mark_too_large"}]}\n'   "$followup_page" > "$PROCESSED/resolution.json"
printf '{"page_id":"%s","source":{"unplaced_pages":[{"rm_page_id":"extra-ink","ink_file_status":"present","line_count":2},{"rm_page_id":"extra-blank","ink_file_status":"absent_assumed_blank","line_count":0}]}}\n'   "$followup_page" > "$PROCESSED/strokes.json"
run_loop render
ok test "$RC" -eq 12
ok grep -q 'ambiguity:stroke-large' "$WORK/err"
ok grep -q 'unplaced-page:extra-ink' "$WORK/err"
ok test "$(grep -c 'extra-blank' "$WORK/err" || true)" -eq 0
ok test "$(manifest_page)" = "$followup_page"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"%s","handled":["notes:already-answered.png","ambiguity:stroke-large","unplaced-page:extra-ink"]}\n'   "$followup_page" > "$ANSWERS"
run_loop render
ok test "$RC" -eq 13
ok grep -q 'NOTHING REMAINS' "$WORK/err"
ok grep -q 'file-processed' "$WORK/err"
# It rendered (that is how it learned nothing remained) but never delivered.
ok test "$(grep -c 'no-generate' "$CALLS" 2>/dev/null || true)" -eq 1
# The zero-card render is a third distinct page, and the loop recorded it.
zero_page="$(manifest_page)"
ok test "$zero_page" != "$followup_page"
ok test "$zero_page" != "$PAGE"
ok grep -q "\"zero_card_page_id\": \"$zero_page\"" "$OUT/loop-state.json"
ok grep -q "\"consumed_page_id\": \"$followup_page\"" "$OUT/loop-state.json"
run_loop status
ok grep -Eq 'consumed: +yes' "$WORK/out"
ok grep -q 'nothing penned is waiting' "$WORK/out"
ok test "$(grep -c 'already-answered.png' "$WORK/out" || true)" -eq 0
rm "$PROCESSED/notes/already-answered.png"


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL for the cross-reviewer finding 3 (mc-j5dr4): file-processed refuses to
# file a document while cards remain.
#
# It used to check consume_gate ONLY. A document can be consumed with every
# card still open - that is the normal state right after the reader pens one answer
# out of five - and filing it then takes his open questions off the device
# with nothing behind them. The bound proof is the manifest for this date,
# which consume_gate has already tied to the same page as the resolution.
#
# Revert the repair and this goes red: the document would be filed and its
# registry entry removed with five cards still open.
# ---------------------------------------------------------------------------
note "NEGATIVE CONTROL: file-processed refuses while cards remain"
saved_manifest="$WORK/manifest-empty.json"
cp "$MANIFEST" "$saved_manifest"
manifest_with row-plain row-defer row-remove row-question row-stale
ok test -e "$DEVICE$PATH_KEY"
run_loop file-processed
ok test "$RC" -eq 14
ok grep -q 'STILL OPEN: 5 card' "$WORK/err"
ok grep -q 'file-processed is for the end of the loop' "$WORK/err"
# Nothing moved and the registry still points at the document.
ok test -e "$DEVICE$PATH_KEY"
ok test ! -e "$DEVICE/Briefs/Previous Briefs/decisions-$DATE"
ok test "$(grep -c '^mv ' "$CALLS" || true)" -eq 0
ok test "$(grep -c '^rm ' "$CALLS" || true)" -eq 0
ok grep -q "$PATH_KEY" "$REGISTRY"

note "NEGATIVE CONTROL: an unreadable manifest is not a zero either"
printf '{"page_id":"%s","cards":' "$PAGE" > "$MANIFEST"
run_loop file-processed
# No card count can be read out of it, so it is not a proof of empty.
ok test "$RC" -eq 14
ok grep -q 'NOT PROVEN EMPTY' "$WORK/err"
ok test -e "$DEVICE$PATH_KEY"
ok test "$(grep -c '^mv ' "$CALLS" || true)" -eq 0
ok grep -q "$PATH_KEY" "$REGISTRY"

note "NEGATIVE CONTROL: a manifest whose card count is unreadable is not a zero"
# "cards" is not a list. len() of a string would have counted its characters
# and len() of a number raises; neither is a card count, and neither is a zero.
printf '{"page_id":"%s","manifest_hash":"mh-x","cards":"lots"}\n' "$PAGE" > "$MANIFEST"
run_loop file-processed
ok test "$RC" -eq 14
ok grep -q 'NOT PROVEN EMPTY' "$WORK/err"
ok test -e "$DEVICE$PATH_KEY"
ok test "$(grep -c '^mv ' "$CALLS" || true)" -eq 0
ok grep -q "$PATH_KEY" "$REGISTRY"
# The same, with a card count that is a bare number rather than a list.
printf '{"page_id":"%s","manifest_hash":"mh-x","cards":4}\n' "$PAGE" > "$MANIFEST"
run_loop file-processed
ok test "$RC" -eq 14
ok grep -q 'NOT PROVEN EMPTY' "$WORK/err"
ok test -e "$DEVICE$PATH_KEY"
ok test "$(grep -c '^mv ' "$CALLS" || true)" -eq 0

# ---------------------------------------------------------------------------
# NEGATIVE CONTROL for the cross-reviewer finding 1 (mc-j5dr4), second half: an empty
# manifest is only a proof if it is the empty manifest THIS loop rendered.
# Revert the repair and this goes green at exit 0: the document is filed and
# its registry entry removed on the word of a manifest nothing here produced.
# ---------------------------------------------------------------------------
note "NEGATIVE CONTROL: a zero-card manifest from another render is not proof"
printf '{"page_id":"%s","manifest_hash":"mh-x","cards":[]}\n' "$PAGE-someone-else" > "$MANIFEST"
run_loop file-processed
ok test "$RC" -eq 14
ok grep -q 'not the one the loop rendered' "$WORK/err"
ok test -e "$DEVICE$PATH_KEY"
ok test "$(grep -c '^mv ' "$CALLS" || true)" -eq 0
ok grep -q "$PATH_KEY" "$REGISTRY"
cp "$saved_manifest" "$MANIFEST"

# ---------------------------------------------------------------------------
# NEGATIVE CONTROL for the cross-reviewer finding 1 (mc-j5dr4), first half: file-processed
# runs on the path where the manifest is the zero-card RE-RENDER, whose page id
# differs from the resolution's. The gate used to compare those two directly
# and exit 11 on its own intended path. Revert the repair and everything below
# here goes red with NOT CONSUMED.
# ---------------------------------------------------------------------------
note "the re-rendered page id differs from the consumed one, and filing still runs"
ok test "$(manifest_page)" != "$(resolution_page)"
ok test "$(resolution_page)" = "$followup_page"
ok test "$(manifest_page)" = "$zero_page"

note "NEGATIVE CONTROL: filing that fails never falls back to deleting"
ok test -e "$DEVICE$PATH_KEY"
DRY_MV_FAILS=1 run_loop file-processed
ok test "$RC" -ne 0
ok grep -q 'FILING FAILED' "$WORK/err"
ok test -e "$DEVICE$PATH_KEY"
ok test "$(grep -c '^rm ' "$CALLS" || true)" -eq 0
# The registry still points at the document, because it is still there.
ok grep -q "$PATH_KEY" "$REGISTRY"

note "filing rechecks unmatched pen marks after the empty render"
cp "$ANSWERS" "$WORK/answers-covered.json"
printf '{"tool":"rmw-decisions-answers-v1","page_id":"%s","handled":["ambiguity:stroke-large"]}\n'   "$followup_page" > "$ANSWERS"
run_loop file-processed
ok test "$RC" -eq 12
ok grep -q 'unplaced-page:extra-ink' "$WORK/err"
ok test -e "$DEVICE$PATH_KEY"
cp "$WORK/answers-covered.json" "$ANSWERS"

note "the processed document is FILED to Previous Briefs, never removed"
run_loop file-processed
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-$DATE"
ok test ! -e "$DEVICE$PATH_KEY"
ok grep -q "^mv $PATH_KEY" "$CALLS"
ok test "$(grep -c '^rm ' "$CALLS" || true)" -eq 0
# Its registry entry went with it, so the next replacement does not wait on a
# document that is no longer in Briefs.
ok test "$(grep -c "$PATH_KEY" "$REGISTRY" || true)" -eq 0

# ---------------------------------------------------------------------------
# NEGATIVE CONTROL for the cross-reviewer finding 4 (mc-j5dr4): a registry step that could
# not be done is a PARTIAL FAILURE, reported with its evidence, not a success.
#
# The device move is irreversible; the registry update is local bookkeeping.
# The old code swallowed an unreadable or absent registry and printed the
# success line over the top of it, so a stale entry pointing at /Briefs for a
# document that had already left survived silently - and the next replacement
# stalled on the harvest gate with nothing to explain why.
#
# Revert the repair and this goes red: the run exits 0 and prints success with
# the registry still holding the stale key.
# ---------------------------------------------------------------------------
note "NEGATIVE CONTROL: an unreadable registry is a partial failure, not a success"
printf 'this is not json\n' > "$REGISTRY"
# The previous case filed the document away, so put one back on the fixture.
printf 'fixture stand-in\n' > "$DEVICE$PATH_KEY"
ok test -e "$DEVICE$PATH_KEY"
run_loop file-processed
ok test "$RC" -eq 15
ok grep -q 'PARTIAL FAILURE' "$WORK/err"
# The evidence is preserved: what moved, and what the registry was expected to hold.
ok grep -q "moved on the device: $PATH_KEY" "$WORK/err"
ok grep -q "expected:  the entry keyed $PATH_KEY" "$WORK/err"
ok grep -q 'could not be parsed' "$WORK/err"
# The move really did happen; the exit does not pretend otherwise.
ok test ! -e "$DEVICE$PATH_KEY"
ok test -e "$DEVICE/Briefs/Previous Briefs/decisions-$DATE"
# and it did NOT print the success line.
ok test "$(grep -c 'Registry entry removed' "$WORK/out" || true)" -eq 0

note "NEGATIVE CONTROL: a registry with no entry for this document is reported too"
printf '{"pages":{}}\n' > "$REGISTRY"
rm -f "$DEVICE/Briefs/Previous Briefs/decisions-$DATE"
run_loop file-processed
ok test "$RC" -eq 15
ok grep -q 'held no entry for' "$WORK/err"
ok test ! -e "$DEVICE$PATH_KEY"

note "NEGATIVE CONTROL: a registry that is a JSON list is reported too"
printf '["not", "an", "object"]\n' > "$REGISTRY"
rm -f "$DEVICE/Briefs/Previous Briefs/decisions-$DATE"
run_loop file-processed
ok test "$RC" -eq 15
ok grep -q "no 'pages' object" "$WORK/err"

rm -f "$DEVICE/Briefs/Previous Briefs/decisions-$DATE"
printf '{"pages":{"%s":{"pushed":true}}}\n' "$PATH_KEY" > "$REGISTRY"

note "NEGATIVE CONTROL: exactly one Decisions document in Briefs"
in_briefs="$(find "$DEVICE/Briefs" -maxdepth 1 -name 'decisions-*' | wc -l | tr -d ' ')"
ok test "$in_briefs" -eq 0
# and after a delivery would have landed one, still exactly one: the loop
# never adds a second, because filing happens before the next push.
touch "$DEVICE/Briefs/decisions-$DATE"
in_briefs="$(find "$DEVICE/Briefs" -maxdepth 1 -name 'decisions-*' | wc -l | tr -d ' ')"
ok test "$in_briefs" -eq 1

note "the dry run never once reached for rmapi rm"
ok test "$(grep -c '^rm ' "$CALLS" || true)" -eq 0

echo
if [ "$FAIL" -eq 0 ]; then
  echo "all decisions-loop tests passed"
else
  echo "decisions-loop tests FAILED"
fi
exit "$FAIL"
