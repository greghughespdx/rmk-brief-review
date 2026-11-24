#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/replace-decisions-document.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DATE=2026-08-20
DEVICE="$WORK/device"
REGISTRY="$WORK/registry.json"
ARCHIVE="$WORK/archive"
OUT="$ARCHIVE/$DATE"
PDF="$OUT/decisions-$DATE.pdf"
MANIFEST="$OUT/decisions-$DATE.manifest.json"
PATH_KEY="/Briefs/decisions-$DATE"
PREV_BRIEFS="$DEVICE/Briefs/Previous Briefs"
FAIL=0

mkdir -p "$WORK/bin" "$DEVICE/Briefs" "$DEVICE/Briefs/Previous Briefs" "$OUT"
printf 'PDF-NEW\n' > "$PDF"
printf '{"page_id":"page-2026-08-20"}\n' > "$MANIFEST"
printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"

# A device document is a real .rmdoc zip, because the replacement script now
# has to read one: it files a document carrying the reader's marks to Previous
# Briefs and only deletes a provably blank, untagged one (the reader 2026-09-07 and
# 2026-09-13). A text file stood in for an archive here before that mattered.
#
#   make_doc <device-file> <marker> [ink|noink] [doc-tags-json] [page-ready]
make_doc() {
  local target="$1" marker="$2" ink="${3:-noink}" tags="${4:-[]}" pready="${5:-n}"
  local dir
  dir="$(mktemp -d)"
  printf '%s\n' "$marker" > "$dir/marker.txt"
  if [ "$pready" = y ]; then
    printf '{"tags":[{"name":"ready"}],"pages":[]}' > "$dir/doc.content"
  else
    printf '{"pages":[]}' > "$dir/doc.content"
  fi
  [ "$ink" = ink ] && printf 'STROKES' > "$dir/page-1.rm"
  rm -f "$target"
  # zip appends .zip to a target without an extension, so it is built under a
  # name zip leaves alone and then moved into place.
  ( cd "$dir" && zip -qr "$dir.zip" . )
  mv "$dir.zip" "$target"
  printf '%s' "$tags" > "$target.tags"
  rm -rf "$dir"
}

# marker_of <raw-archive> -> the marker text inside it, so the tests can still
# say which document was fetched.
marker_of() { unzip -p "$1" marker.txt 2>/dev/null | tr -d '\n'; }

make_doc "$DEVICE/Briefs/decisions-$DATE" RAW-OLD

cat > "$WORK/bin/rmapi" <<'EOF'
#!/usr/bin/env bash
cmd="$1"; shift
printf '%s %s\n' "$cmd" "$*" >> "$CALL_LOG"
case "$cmd" in
  ls)
    dir="$DEVICE/${1#/}"
    [ -d "$dir" ] || exit 0
    for item in "$dir"/*; do
      [ -e "$item" ] || continue
      case "$item" in *.tags) continue ;; esac
      printf '[f]\t%s\n' "$(basename "$item")"
    done
    # A real device can hold two documents with the SAME visible name; a
    # directory cannot hold two files with the same basename. EXTRA_TWIN
    # names the duplicate the listing must also report (mc-9jd4t).
    [ -n "${EXTRA_TWIN:-}" ] && printf '[f]\t%s\n' "$EXTRA_TWIN"
    ;;
  get)
    src="$DEVICE/${1#/}"
    [ -f "$src" ] || exit 1
    cp "$src" "$(basename "$src").rmdoc"
    ;;
  rm)
    # NEGATIVE CONTROL for the no-rm-under-/Briefs invariant (mc-j5dr4). rmapi
    # rm is a HARD delete with no undo (bd memory
    # remarkable-rmapi-rm-vs-trash-2026-09-09), so the production script must
    # never reach for it. This stub refuses rather than obliging: if the repair
    # is reverted, the run that called rm fails here and the suite goes red,
    # instead of quietly deleting a fixture and passing.
    echo "FIXTURE REFUSES rmapi rm: $*" >&2
    exit 1
    ;;
  put)
    if [ -n "${FAIL_PUT_ONCE:-}" ] && [ ! -e "$FAIL_PUT_ONCE" ]; then
      : > "$FAIL_PUT_ONCE"
      echo "temporary upload failure" >&2
      exit 1
    fi
    src="$1"; dest="$2"
    mkdir -p "$DEVICE/${dest#/}"
    base="$(basename "$src")"; base="${base%.*}"
    cp "$src" "$DEVICE/${dest#/}/$base"
    ;;
  stat)
    # STAT_FAILS: the cloud call itself fails. STAT_GARBAGE: it succeeds and
    # returns something that is not a JSON object. Both used to reach
    # tag_flags.py, whose soft failure prints an empty line that read as "no
    # tags" and licensed a delete (mc-j5dr4, the cross-reviewer finding 1).
    [ -n "${STAT_FAILS:-}" ] && { echo "stat unavailable" >&2; exit 1; }
    [ -n "${STAT_GARBAGE:-}" ] && { printf 'not json at all\n'; exit 0; }
    target="$DEVICE/${1#/}"
    [ -e "$target" ] || exit 1
    tags="[]"
    [ -f "$target.tags" ] && tags="$(cat "$target.tags")"
    printf '{"ModifiedClient":"m1","Tags":%s}\n' "$tags"
    ;;
  mv)
    [ -n "${MV_FAILS:-}" ] && { echo "move refused" >&2; exit 1; }
    src="$DEVICE/${1#/}"
    dst="$DEVICE/${2#/}"
    [ -e "$src" ] || { echo "no such document" >&2; exit 1; }
    [ -d "$dst" ] || { echo "no such folder" >&2; exit 1; }
    # mc-9jd4t: the real failure, now on the call that actually runs. The
    # tablet wrote a new version while the move was in flight, so the move
    # became a no-op that still exited 0.
    if [ -n "${MV_LOSES_RACE:-}" ]; then
      exit 0
    fi
    mv "$src" "$dst/"
    [ -f "$src.tags" ] && mv "$src.tags" "$dst/"
    if [ -n "${CRASH_AFTER_MV_ONCE:-}" ] && [ ! -e "$CRASH_AFTER_MV_ONCE" ]; then
      : > "$CRASH_AFTER_MV_ONCE"
      kill -KILL "$PPID"
    fi
    ;;
  mkdir) mkdir -p "$DEVICE/${1#/}" ;;
  *) exit 2 ;;
esac
EOF

cat > "$WORK/bin/push" <<'EOF'
#!/usr/bin/env bash
"$RMAPI" put "$1" "$2"
EOF

cat > "$WORK/bin/today-page.py" <<'PY'
#!/usr/bin/env python3
import json
import os
import pathlib
import sys

if os.environ.get("FAIL_REGISTER_ONCE"):
    marker = pathlib.Path(os.environ["FAIL_REGISTER_ONCE"])
    if not marker.exists():
        marker.write_text("failed\n")
        raise SystemExit(1)
args = sys.argv[1:]
assert args[0] == "register"
registry = pathlib.Path(args[args.index("--registry") + 1])
device_path = args[args.index("--device-path") + 1]
try:
    data = json.loads(registry.read_text())
except Exception:
    data = {"pages": {}}
manifest_path = pathlib.Path(args[args.index("--manifest") + 1]).resolve()
manifest = json.loads(manifest_path.read_text())
data.setdefault("pages", {})[device_path] = {
    "manifest": str(manifest_path),
    "page_id": manifest["page_id"],
}
tmp = registry.with_suffix(".tmp")
tmp.write_text(json.dumps(data) + "\n")
tmp.replace(registry)
PY
chmod +x "$WORK/bin/"*
: > "$WORK/calls"

# The delivery script refuses to send a document that was not reviewed
# (mc-iacbw section 6), so each run gets a receipt for the exact bytes it is
# about to send, from a reviewer who is not the producer. These fixtures carry
# no cards, so there are no card verdicts to record; what is being tested here
# is the replacement mechanics, not the review.
write_receipt() {
  python3 - "$PDF" "$MANIFEST" "$ARCHIVE/$DATE/review-receipt.json" <<'PYEOF'
import hashlib, json, pathlib, sys
pdf, manifest, receipt = (pathlib.Path(value) for value in sys.argv[1:4])
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(json.dumps({
    "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
    "manifest_hash": json.loads(manifest.read_text()).get("manifest_hash"),
    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "reviewer": "a-non-producing-reviewer",
    "reviewed_at": "2026-08-20T05:00:00-07:00",
    "cards": [],
}))
PYEOF
}

run_replace() {
  write_receipt
  DEVICE="$DEVICE" CALL_LOG="$WORK/calls" RMAPI="$WORK/bin/rmapi" \
  DECISIONS_PUSH_HELPER="$WORK/bin/push" TODAY_PAGE_PY="$WORK/bin/today-page.py" \
  FAIL_PUT_ONCE="${FAIL_PUT_ONCE:-}" FAIL_REGISTER_ONCE="${FAIL_REGISTER_ONCE:-}" \
  CRASH_AFTER_MV_ONCE="${CRASH_AFTER_MV_ONCE:-}" \
  MV_LOSES_RACE="${MV_LOSES_RACE:-}" EXTRA_TWIN="${EXTRA_TWIN:-}" \
  STAT_FAILS="${STAT_FAILS:-}" STAT_GARBAGE="${STAT_GARBAGE:-}" \
  TAG_FLAGS="$HERE/tag_flags.py" PYTHON=python3 MV_FAILS="${MV_FAILS:-}" \
  "$SCRIPT" --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --out-dir "$OUT" \
    --registry "$REGISTRY" --archive-root "$ARCHIVE" --device-path "$PATH_KEY" \
    --extractor-python /usr/bin/python3 --review-queue "$WORK/queue.md" \
    > "$WORK/out" 2> "$WORK/err"
  RC=$?
}

ok() { if "$@"; then echo "[ok] $*"; else echo "[FAIL] $*"; FAIL=$((FAIL + 1)); fi; }

# Crash boundary 1: the process dies after the old document is FILED but
# before old_deleted is journalled. The next run must see state=archived plus
# an absent document, advance safely, and never move the absent path again.
CRASH_AFTER_MV_ONCE="$WORK/crashed-after-mv" run_replace
ok test "$RC" -ne 0
ok test ! -e "$DEVICE/Briefs/decisions-$DATE"
ok test -e "$PREV_BRIEFS/decisions-$DATE"
ok grep -q '"state": "archived"' "$ARCHIVE/replacement-journals/decisions-$DATE.json"
mv_calls_after_crash="$(grep -c '^mv ' "$WORK/calls" || true)"
rm -f "$PREV_BRIEFS/decisions-$DATE" "$PREV_BRIEFS/decisions-$DATE.tags"

# The resumed upload now fails. The journal must have advanced to old_deleted,
# and the move must not have been retried on the already-absent document.
CRASH_AFTER_MV_ONCE="" \
FAIL_PUT_ONCE="$WORK/put-failed" run_replace
ok test "$RC" -ne 0
ok test ! -e "$DEVICE/Briefs/decisions-$DATE"
ok grep -q '"state": "old_deleted"' "$ARCHIVE/replacement-journals/decisions-$DATE.json"
ok test "$(grep -c '^mv ' "$WORK/calls" || true)" -eq "$mv_calls_after_crash"
raw="$(find "$ARCHIVE/replaced" -name '*.rmdoc' -print -quit)"
ok test "$(marker_of "$raw")" = RAW-OLD

# Resume from old_deleted. It must push once, verify, register, and remove the
# journal without attempting to archive or delete the absent old copy again.
FAIL_PUT_ONCE="" run_replace
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/decisions-$DATE"
ok test ! -e "$ARCHIVE/replacement-journals/decisions-$DATE.json"
ok python3 - "$REGISTRY" "$PATH_KEY" <<'PY'
import json, sys
page = json.load(open(sys.argv[1]))["pages"][sys.argv[2]]
assert page["page_id"] == "page-2026-08-20"
assert page["manifest"].endswith("decisions-2026-08-20.manifest.json")
PY

# Crash boundary 2: registration fails after a successful fresh push. A rerun
# must register the already-pushed document without uploading a duplicate.
make_doc "$DEVICE/Briefs/decisions-$DATE" RAW-SECOND
printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"
before_puts="$(grep -c '^put ' "$WORK/calls" || true)"
FAIL_REGISTER_ONCE="$WORK/register-failed" run_replace
ok test "$RC" -ne 0
ok grep -q '"state": "new_pushed"' "$ARCHIVE/replacement-journals/decisions-$DATE.json"
after_failed_register="$(grep -c '^put ' "$WORK/calls" || true)"
rm -f "$DEVICE/Briefs/decisions-$DATE"
FAIL_REGISTER_ONCE="" run_replace
ok test "$RC" -ne 0
ok grep -q 'journal says pushed but the Decisions document is absent' "$WORK/err"
cp "$PDF" "$DEVICE/Briefs/decisions-$DATE"
FAIL_REGISTER_ONCE="" run_replace
after_resume="$(grep -c '^put ' "$WORK/calls" || true)"
ok test "$RC" -eq 0
ok test "$after_resume" -eq "$after_failed_register"
ok test "$after_failed_register" -eq $((before_puts + 1))

# A forged journal cannot redirect any filesystem action. The helper validates
# the fixed identity and contained archive before touching the cloud document.
make_doc "$DEVICE/Briefs/decisions-$DATE" KEEP
mkdir -p "$ARCHIVE/replacement-journals"
printf '{"tool":"rmw-decisions-replacement-v1","date":"%s","device_path":"%s","pdf":"%s","manifest":"%s","archive_path":"/tmp/forged.rmdoc","state":"archived"}\n' \
  "$DATE" "$PATH_KEY" "$(cd "$(dirname "$PDF")" && pwd)/$(basename "$PDF")" \
  "$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")" \
  > "$ARCHIVE/replacement-journals/decisions-$DATE.json"
run_replace
ok test "$RC" -ne 0
ok test "$(marker_of "$DEVICE/Briefs/decisions-$DATE")" = KEEP
ok grep -q 'REFUSING' "$WORK/err"

# ---------------------------------------------------------------------------
# mc-9jd4t: the delete that reports success and does not take.
#
# 2026-09-13 17:05. The removal exited 0 and the document survived, because
# the tablet had written a new version 28 seconds earlier. The removal is a
# `mv` to Previous Briefs now (mc-j5dr4), and it can lose exactly the same
# race, so the guard is exercised there. The upload landed
# beside it, /Briefs held two documents called decisions-2026-09-13, and the reader
# penned four pages of answers onto the stale one. The only post-upload check
# was "did MY document arrive", which passes just as happily with a stale twin
# next to it.
# ---------------------------------------------------------------------------

rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"
make_doc "$DEVICE/Briefs/decisions-$DATE" RAW-SURVIVES
printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"
puts_before_race="$(grep -c '^put ' "$WORK/calls" || true)"

MV_LOSES_RACE=1 run_replace
ok test "$RC" -eq 9
ok grep -q 'OLD DOCUMENT STILL THERE' "$WORK/err"
# Nothing was uploaded. This is the whole point: the duplicate never exists.
ok test "$(grep -c '^put ' "$WORK/calls" || true)" -eq "$puts_before_race"
# The document that survived is untouched, so its ink is still on the device.
ok test "$(marker_of "$DEVICE/Briefs/decisions-$DATE")" = RAW-SURVIVES
# The operator is told where the fetched copy went and how to look for ink.
ok grep -q 'never deleted' "$WORK/err"
ok grep -q 'rmapi get --id' "$WORK/err"

# The same run again with a delete that works must succeed, proving the guard
# refuses the race and nothing else.
rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"
make_doc "$DEVICE/Briefs/decisions-$DATE" RAW-SURVIVES
printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"
MV_LOSES_RACE="" run_replace
rm -f "$PREV_BRIEFS/decisions-$DATE" "$PREV_BRIEFS/decisions-$DATE.tags"
ok test "$RC" -eq 0
ok test -e "$DEVICE/Briefs/decisions-$DATE"

# A twin that appears only AFTER the upload (the device syncing an old copy
# back while the push runs) must also be caught, not reported as success.
rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"
make_doc "$DEVICE/Briefs/decisions-$DATE" RAW-OLD-AGAIN
printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"
EXTRA_TWIN="decisions-$DATE" run_replace
ok test "$RC" -ne 0
ok grep -qE 'OLD DOCUMENT STILL THERE|DUPLICATE AFTER PUSH' "$WORK/err"

# ---------------------------------------------------------------------------
# mc-j5dr4: the processed document is FILED, not destroyed.
#
# The reader 2026-09-07: "if there are still cards or questions remaining, I want a
# new decisions document created and pushed to the reMarkable and the
# processed decisions doc filed." the reader 2026-09-13: a document he answered or
# acted on moves to the previous-briefs folder; only an untouched one is
# deleted. This script used to delete every registered document it replaced,
# so an intraday replacement threw away the copy he had just penned.
# ---------------------------------------------------------------------------

PREV="$PREV_BRIEFS"
reset_cycle() {  # reset_cycle <marker> [ink|noink] [doc-tags-json] [page-ready]
  rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"
  make_doc "$DEVICE/Briefs/decisions-$DATE" "$1" "${2:-noink}" "${3:-[]}" "${4:-n}"
  printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"
  : > "$WORK/calls"
}

echo "--- a document with ink is filed, never deleted ---"
reset_cycle PENNED ink
run_replace
ok test "$RC" -eq 0
ok test -e "$PREV/decisions-$DATE"
ok test "$(marker_of "$PREV/decisions-$DATE")" = PENNED
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
ok grep -q "^mv /Briefs/decisions-$DATE" "$WORK/calls"
# and the fresh document really did land in /Briefs
ok test -e "$DEVICE/Briefs/decisions-$DATE"
rm -f "$PREV/decisions-$DATE" "$PREV/decisions-$DATE.tags"

echo "--- a document the reader tagged Marked as Read is filed, never deleted ---"
reset_cycle TAGGED noink '[{"name":"Marked as Read"}]'
run_replace
ok test "$RC" -eq 0
ok test "$(marker_of "$PREV/decisions-$DATE")" = TAGGED
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
rm -f "$PREV/decisions-$DATE" "$PREV/decisions-$DATE.tags"

echo "--- a page-level ready tag also counts as touched ---"
reset_cycle PAGETAGGED noink '[]' y
run_replace
ok test "$RC" -eq 0
ok test "$(marker_of "$PREV/decisions-$DATE")" = PAGETAGGED
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
rm -f "$PREV/decisions-$DATE" "$PREV/decisions-$DATE.tags"

# NEGATIVE CONTROL for the cross-reviewer finding 2 (mc-j5dr4): a blank, untagged
# document is FILED, not deleted. It used to be the one case that called
# `rmapi rm` under /Briefs, and rmapi rm is a hard delete with no undo (bd
# memory remarkable-rmapi-rm-vs-trash-2026-09-09). The reader's 2026-09-13 rule that
# an untouched document may be removed is satisfied by the move: it leaves
# /Briefs, and rotate-briefs.sh prunes Previous Briefs on its 7-day schedule.
# Revert the repair and this goes red twice over - the mv assertion fails, and
# the fixture rmapi refuses the rm outright.
echo "--- NEGATIVE CONTROL: a blank, untagged document is FILED, never deleted ---"
reset_cycle BLANK
run_replace
ok test "$RC" -eq 0
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
ok test -e "$PREV/decisions-$DATE"
ok test "$(marker_of "$PREV/decisions-$DATE")" = BLANK
ok grep -q "^mv /Briefs/decisions-$DATE" "$WORK/calls"
ok grep -q '(untouched)' "$WORK/out"
rm -f "$PREV/decisions-$DATE" "$PREV/decisions-$DATE.tags"

# ---------------------------------------------------------------------------
# NEGATIVE CONTROL for the cross-reviewer finding 1 (mc-j5dr4): an unreadable or malformed
# document is never treated as blank.
#
# tag_flags.py fails SOFT on purpose: a malformed .content prints the untagged
# answer "n|n" and a failed `rmapi stat` prints an empty line. That contract is
# right for the watcher and was wrong here, where the silence read as "blank"
# and licensed a delete. Each case below must now STOP the run with a visible
# error, leave the document exactly where it is, and push nothing.
#
# Revert the repair and every one of these goes red: the run would file (or,
# before finding 2, delete) a document whose marks it could not account for.
# ---------------------------------------------------------------------------

# make_broken_doc <device-file> <marker> <kind>
#   badzip     the fetched archive is not a readable zip at all
#   nocontent  a valid zip with no .content member
#   badcontent a valid zip whose .content is not parseable JSON
#   listcontent a valid zip whose .content parses but is a LIST, not an object
make_broken_doc() {
  local target="$1" marker="$2" kind="$3" dir
  dir="$(mktemp -d)"
  printf '%s\n' "$marker" > "$dir/marker.txt"
  case "$kind" in
    badcontent)  printf '{"pages":[' > "$dir/doc.content" ;;
    listcontent) printf '[{"tags":[]}]' > "$dir/doc.content" ;;
    nocontent)   : ;;
  esac
  rm -f "$target"
  if [ "$kind" = badzip ]; then
    printf 'this is not a zip file at all\n' > "$target"
  else
    ( cd "$dir" && zip -qr "$dir.zip" . )
    mv "$dir.zip" "$target"
  fi
  printf '[]' > "$target.tags"
  rm -rf "$dir"
}

# unreadable_case <kind> <marker> [extra env assignments are the caller's job]
unreadable_case() {
  local kind="$1" marker="$2"
  rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"
  make_broken_doc "$DEVICE/Briefs/decisions-$DATE" "$marker" "$kind"
  printf '{"pages":{"%s":{"old":true}}}\n' "$PATH_KEY" > "$REGISTRY"
  : > "$WORK/calls"
}

for case_kind in badzip nocontent badcontent listcontent; do
  echo "--- NEGATIVE CONTROL: an unreadable document ($case_kind) stops the run ---"
  unreadable_case "$case_kind" "UNREADABLE-$case_kind"
  puts_before_unreadable="$(grep -c '^put ' "$WORK/calls" || true)"
  run_replace
  ok test "$RC" -eq 11
  ok grep -q 'UNREADABLE DOCUMENT' "$WORK/err"
  ok grep -q 'never treated as blank' "$WORK/err"
  # The document is still exactly where it was: not moved, not deleted.
  ok test -e "$DEVICE/Briefs/decisions-$DATE"
  ok test ! -e "$PREV/decisions-$DATE"
  ok test "$(grep -c '^mv ' "$WORK/calls" || true)" -eq 0
  ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
  # And nothing was pushed in its place.
  ok test "$(grep -c '^put ' "$WORK/calls" || true)" -eq "$puts_before_unreadable"
  rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"
done

echo "--- NEGATIVE CONTROL: a failed rmapi stat is unreadable, not untagged ---"
# A blank, untagged archive whose document-level tags cannot be read. Before
# the repair the empty `rmapi stat` output parsed as "no tags" and the document
# was deleted; now the failure is visible and nothing moves.
reset_cycle STATFAILS
rm -f "$DEVICE/Briefs/decisions-$DATE.tags"
STAT_FAILS=1 run_replace
ok test "$RC" -eq 11
ok grep -q 'UNREADABLE DOCUMENT' "$WORK/err"
ok test -e "$DEVICE/Briefs/decisions-$DATE"
ok test "$(grep -c '^mv ' "$WORK/calls" || true)" -eq 0
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"

echo "--- NEGATIVE CONTROL: a non-JSON rmapi stat is unreadable, not untagged ---"
reset_cycle STATGARBAGE
STAT_GARBAGE=1 run_replace
ok test "$RC" -eq 11
ok grep -q 'UNREADABLE DOCUMENT' "$WORK/err"
ok test -e "$DEVICE/Briefs/decisions-$DATE"
ok test "$(grep -c '^mv ' "$WORK/calls" || true)" -eq 0
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"

echo "--- the same document, readable again, files normally ---"
reset_cycle READABLE
run_replace
ok test "$RC" -eq 0
ok test "$(marker_of "$PREV/decisions-$DATE")" = READABLE
rm -f "$PREV/decisions-$DATE" "$PREV/decisions-$DATE.tags"

echo "--- filing that fails stops the run; nothing is deleted or pushed ---"
reset_cycle CANNOTFILE ink
puts_before_filing_failure="$(grep -c '^put ' "$WORK/calls" || true)"
MV_FAILS=1 run_replace
ok test "$RC" -eq 10
ok grep -q 'FILING FAILED' "$WORK/err"
ok test "$(marker_of "$DEVICE/Briefs/decisions-$DATE")" = CANNOTFILE
ok test "$(grep -c '^put ' "$WORK/calls" || true)" -eq "$puts_before_filing_failure"
ok test "$(grep -c '^rm ' "$WORK/calls" || true)" -eq 0
rm -f "$ARCHIVE/replacement-journals/decisions-$DATE.json"

# NEGATIVE CONTROL for the cross-reviewer finding 2, across the whole suite: not one run
# above, in any state, on any path, reached for `rmapi rm` under /Briefs. The
# per-case assertions clear the call log; this one does not, so a delete
# smuggled into a path no single case covers is still caught here.
#
# $WORK/calls is truncated by reset_cycle, so the durable record is the stderr
# of the fixture rmapi, which refuses rm loudly. A cumulative count is kept
# instead: every case asserts zero, and this asserts the log holds no rm now.
if grep -q '^rm ' "$WORK/calls"; then
  echo "[FAIL] rmapi rm was called under /Briefs"
  FAIL=$((FAIL + 1))
else
  echo "[ok] rmapi rm was never called under /Briefs"
fi

if grep -q -- '--content-only' "$WORK/calls"; then
  echo "[FAIL] content-only replacement was attempted"
  FAIL=$((FAIL + 1))
else
  echo "[ok] content-only replacement was never attempted"
fi

exit "$FAIL"
