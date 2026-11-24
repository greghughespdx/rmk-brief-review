#!/usr/bin/env bash
# Binding tests for reMarkable tag handling (mc-k1tr ready, mc-ylap mark-as-read).
#
# HISTORY, because it determines the shape of this file. The first version of
# these tests substituted the rmapi calls at a point AFTER tag parsing and then
# reimplemented the normaliser inside the test. The cross-reviewer proved the consequence
# by mutation: he changed both production comparisons to match 'nevermatch' and
# every case still passed, banner included. The tests were green against code
# that could not work.
#
# So this file is built around one rule: exercise the PRODUCTION parsers with
# RAW payloads. Section 1 feeds real `rmapi stat` JSON and real `.content` JSON
# through tag_flags.py, which is the exact module check-annotations.sh invokes.
# Section 2 drives the real state machine. Section 3 is the mutation control
# that would have caught the original defect - it points the watcher at a broken
# copy of the matcher and REQUIRES the suite to fail.
#
# Usage: remarkable/test-annotation-tags.sh
# Exit 0 = everything holds. Exit 1 = a regression.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/check-annotations.sh"
TAG_FLAGS_SRC="$HERE/tag_flags.py"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAILURES=0
note_fail() { echo "  [FAIL] $1"; FAILURES=$((FAILURES + 1)); }

# ---------------------------------------------------------------------------
# Section 1: the production parsers, fed raw payloads.
# ---------------------------------------------------------------------------
echo "production tag parsers (tag_flags.py, the module the watcher invokes):"

# parse <mode> <json> -> stdout
parse() { printf '%s' "$2" | python3 "${TAG_FLAGS:-$TAG_FLAGS_SRC}" "$1"; }

expect() {
  local name="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then echo "  [ ok ] $name"; else
    note_fail "$name: got [$got] want [$want]"; fi
}

# Document level, via an rmapi stat payload.
expect "stat: no tags" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":[]}')" "M1	n	n"
expect "stat: ready only" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":["ready"]}')" "M1	y	n"
expect "stat: Mark-as-Read as the reader dictated it" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":["Mark-as-Read"]}')" "M1	n	y"
expect "stat: both tags together" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":["ready","Mark-as-Read"]}')" "M1	y	y"
# The spelling the reader actually uses on the device (observed 2026-09-09 on a document
# he tagged 2026-09-04; mc-1gx9o). It went unrecognised for three weeks.
expect "stat: Marked as Read, the spelling the reader really uses" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":["Marked as Read"]}')" "M1	n	y"
expect "stat: marked_as_read separator form" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":["marked_as_read"]}')" "M1	n	y"
expect "stat: a near miss is not a read receipt" \
  "$(parse stat '{"ModifiedClient":"M1","Tags":["marked as unread"]}')" "M1	n	n"
expect "stat: no ModifiedClient yields empty" \
  "$(parse stat '{"Tags":["ready"]}')" ""
expect "stat: unparseable input is not a tag" \
  "$(parse stat 'not json at all')" ""

# VALID JSON, WRONG SHAPE. The cross-reviewer's R3 blocker: the fail-soft boundary used to
# cover syntax only, so these exited non-zero with no output - which erased the
# document from the state table and deleted its stored row. Every one of these
# must print the untagged answer and exit 0.
shape_soft() {
  local name="$1" mode="$2" payload="$3" want="$4"
  local got rc
  got=$(printf '%s' "$payload" | python3 "${TAG_FLAGS:-$TAG_FLAGS_SRC}" "$mode" 2>"$WORK/err.txt"); rc=$?
  if [ "$got" = "$want" ] && [ "$rc" = 0 ] && [ ! -s "$WORK/err.txt" ]; then
    echo "  [ ok ] $name"
  else
    note_fail "$name: got [$got] rc=$rc stderr=[$(cat "$WORK/err.txt")] want [$want] rc=0 no stderr"
  fi
}
shape_soft "stat: top-level list"            stat '[]'                              ""
shape_soft "stat: top-level null"            stat 'null'                            ""
shape_soft "stat: top-level string"          stat '"nope"'                          ""
shape_soft "stat: Tags is a string"          stat '{"ModifiedClient":"M1","Tags":"ready"}'  "M1	n	n"
shape_soft "stat: Tags holds bare strings"   stat '{"ModifiedClient":"M1","Tags":["ready"]}' "M1	y	n"
shape_soft "stat: ModifiedClient not a string" stat '{"ModifiedClient":123,"Tags":[]}' ""
shape_soft "content: top-level list"         content '[]'                           "n|n"
shape_soft "content: top-level null"         content 'null'                         "n|n"
shape_soft "content: tags holds bare strings" content '{"tags":["Mark-as-Read"]}'    "n|y"
shape_soft "content: tags is a dict"         content '{"tags":{"name":"ready"}}'     "n|n"
shape_soft "content: tag item name not a string" content '{"tags":[{"name":42}]}'    "n|n"
shape_soft "content: pageTags is a number"   content '{"tags":[],"pageTags":7}'      "n|n"

# Page level, via a .content payload. This parser was never executed at all by
# the previous suite.
expect "content: no tags" \
  "$(parse content '{"tags":[],"pageTags":[]}')" "n|n"
expect "content: doc-level ready" \
  "$(parse content '{"tags":[{"name":"ready"}],"pageTags":[]}')" "y|n"
expect "content: PAGE-level ready" \
  "$(parse content '{"tags":[],"pageTags":[{"name":"ready"}]}')" "y|n"
expect "content: PAGE-level Mark-as-Read" \
  "$(parse content '{"tags":[],"pageTags":[{"name":"Mark-as-Read"}]}')" "n|y"
expect "content: doc-level mark-as-read, page-level ready" \
  "$(parse content '{"tags":[{"name":"mark_as_read"}],"pageTags":[{"name":"ready"}]}')" "y|y"
expect "content: unparseable input is not a tag" \
  "$(parse content 'not json')" "n|n"

# Spelling tolerance. The reader's real tag, seen on the device 2026-09-09, is
# "Marked as Read" (mc-1gx9o); the dictated "Mark-as-Read" is kept. Any
# case/separator form of either is accepted. The payload is built with printf
# for simpler construction; the test defect this fixes is that marked-as-read
# sat in the rejection set below until this change. (Observed under bash -x on
# the previous inline form: the parser received the payload without its outer
# braces, so the rejection cases were answering on invalid JSON; the printf
# form makes the payload visible in the trace as written.)
tag_payload() { printf '{"tags":[{"name":"%s"}],"pageTags":[]}' "$1"; }
for form in 'Mark-as-Read' 'mark-as-read' 'markasread' 'MARKASREAD' 'mark_as_read' 'Mark As Read' \
            'Marked as Read' 'marked-as-read' 'MARKED AS READ' 'marked_as_read'; do
  expect "spelling accepted: $form" \
    "$(parse content "$(tag_payload "$form")")" "n|y"
done
# Things that must NOT be read as the tag.
for form in 'read' 'mark-as-read-later' 'unread' 'marked as unread' 'marked'; do
  expect "spelling rejected: $form" \
    "$(parse content "$(tag_payload "$form")")" "n|n"
done
# 'ready' is matched case-insensitively but NOT normalised.
expect "ready is not normalised: 're-ady' is not ready" \
  "$(parse content '{"tags":[{"name":"re-ady"}],"pageTags":[]}')" "n|n"
expect "ready accepts case: READY" \
  "$(parse content '{"tags":[{"name":"READY"}],"pageTags":[]}')" "y|n"

# ---------------------------------------------------------------------------
# Section 2: the real state machine.
# ---------------------------------------------------------------------------
# Only the two rmapi network calls are substituted, and the substitution now
# happens BEFORE parsing: the stage-1 stub emits raw stat JSON and the fetch
# stub emits a real .content payload, so tag_flags.py runs in both paths.
build_harness() {
  python3 - "$1" "$2" <<'PY'
import sys
src, out = sys.argv[1], sys.argv[2]
s = open(src).read()
# Stage 1: only the rmapi enumeration is replaced. The fixture supplies RAW
# stat JSON and the PRODUCTION stage1_row function decides what to emit,
# including the unreadable-payload marker.
#
# Substituting the stage1_scan FUNCTION, not a spliced region of the file.
# The old version cut from the first `sort -u "$TREES_FILE"` to the next
# `> "$TMP_NEW"`; when mc-rsh8 split stage 1 into two scanners those anchors
# straddled a function boundary and the harness produced a file with a
# dangling `else`. A named seam cannot drift like that.
anchor = '''stage1_scan() {
  if [ "${RM_STAGE1_JSON:-0}" = "1" ]; then'''
assert anchor in s, "stage1_scan seam missing from check-annotations.sh"
s = s.replace(anchor, '''stage1_scan() {
  while IFS=$'\\t' read -r _p _json; do
    [ -z "$_p" ] && continue
    printf '%s' "$_json" | stage1_row "$_p"
  done < "$FIXTURE_STAGE1"
  return
}
_real_stage1_scan() {
  if [ "${RM_STAGE1_JSON:-0}" = "1" ]; then''')
# fetch_doc, but the fixture supplies a RAW .content payload, parsed for real.
s = s.replace('''fetch_doc() {
  local dir zip h p''', '''fetch_doc() {
  local _row _hash _content
  _row=$(grep -F "$1"$'\\t' "$FIXTURE_FETCH" | head -1)
  _hash=$(printf '%s' "$_row" | cut -f2)
  _content=$(printf '%s' "$_row" | cut -f3)
  echo "$_hash|$(printf '%s' "$_content" | python3 "$TAG_FLAGS" content)"
  return
}
_real_fetch_doc() {
  local dir zip h p''')
s = s.replace('STATE=~/.remarkable-annotation-state.json',
              'STATE="${STATE:-$HOME/.remarkable-annotation-state.json}"')
s = s.replace('LOG=/tmp/remarkable-annotation-watch.log',
              'LOG="${LOG:-/tmp/remarkable-annotation-watch.log}"')
open(out, 'w').write(s)
PY
}
build_harness "$SCRIPT" "$WORK/harness.sh"

case_run() {
  local name="$1" state="$2" stage1="$3" fetch="$4" want_fire="$5" want_log="$6" mode="${7:-check}"
  printf '%b' "$state"  > "$WORK/state.tsv"
  printf '%b' "$stage1" > "$WORK/stage1.tsv"
  printf '%b' "$fetch"  > "$WORK/fetch.tsv"
  : > "$WORK/processed.tsv"
  : > "$WORK/log.txt"
  local fired logged ok=1
  fired=$(STATE="$WORK/state.tsv" PROCESSED_STATE="$WORK/processed.tsv" LOG="$WORK/log.txt" \
          TAG_FLAGS="${TAG_FLAGS:-$TAG_FLAGS_SRC}" \
          FIXTURE_STAGE1="$WORK/stage1.tsv" FIXTURE_FETCH="$WORK/fetch.tsv" \
          bash "$WORK/harness.sh" "$mode" 2>&1)
  logged=$(cat "$WORK/log.txt")
  [ "$fired" = "$want_fire" ] || ok=0
  if [ "$want_log" = "-" ]; then [ -z "$logged" ] || ok=0
  else printf '%s' "$logged" | grep -q "$want_log" || ok=0; fi
  if [ "$ok" = 1 ]; then echo "  [ ok ] $name"; else
    note_fail "$name"; echo "         fired want=[$want_fire] got=[$fired]"
    echo "         log   want=[$want_log] got=[$logged]"; fi
}

echo "state machine (real parsers, raw payloads):"

case_run "doc-level mark-as-read logs a receipt and fires nothing" \
  '/Briefs/a\tM1\tH1\tn\tn\n' \
  '/Briefs/a\t{"ModifiedClient":"M2","Tags":["Mark-as-Read"]}\n' \
  '/Briefs/a\tH1\t{"tags":[],"pageTags":[]}\n' \
  '' 'read-receipt'

case_run "PAGE-level mark-as-read also logs a receipt" \
  '/Briefs/p\tM1\tH1\tn\tn\n' \
  '/Briefs/p\t{"ModifiedClient":"M2","Tags":[]}\n' \
  '/Briefs/p\tH2\t{"tags":[],"pageTags":[{"name":"Mark-as-Read"}]}\n' \
  '' 'read-receipt'

case_run "both tags: receipt logged AND processing fires" \
  '/Briefs/b\tM1\tH1\tn\tn\n' \
  '/Briefs/b\t{"ModifiedClient":"M2","Tags":["ready","Mark-as-Read"]}\n' \
  '/Briefs/b\tH2\t{"tags":[],"pageTags":[]}\n' \
  '/Briefs/b' 'read-receipt'

case_run "held mark-as-read + new ink logs no duplicate receipt" \
  '/Briefs/c\tM1\tH1\tn\ty\n' \
  '/Briefs/c\t{"ModifiedClient":"M2","Tags":["Mark-as-Read"]}\n' \
  '/Briefs/c\tH2\t{"tags":[],"pageTags":[]}\n' \
  '' 'quiet: ink changed'

case_run "ready alone still fires, no receipt" \
  '/Briefs/d\tM1\tH1\tn\tn\n' \
  '/Briefs/d\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/d\tH2\t{"tags":[],"pageTags":[]}\n' \
  '/Briefs/d' '-'

# mc-1nr9d review (the cross-reviewer): the n-to-y transition above is not the only real
# state. Ready held across a run (not newly applied) must behave on its own
# terms: unchanged metadata never fires, and new ink while ready is still
# present fires exactly like a transition does (check-annotations.sh's
# "unchanged metadata" carry-forward and its "new ink while tag present"
# branch, both independent of any transition).
case_run "held ready + unchanged metadata produces no fire" \
  '/Briefs/h\tM1\tH1\ty\tn\n' \
  '/Briefs/h\t{"ModifiedClient":"M1","Tags":["ready"]}\n' \
  '/Briefs/h\tH1\t{"tags":[],"pageTags":[]}\n' \
  '' '-'

case_run "held ready + changed ink hash fires once" \
  '/Briefs/i\tM1\tH1\ty\tn\n' \
  '/Briefs/i\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/i\tH2\t{"tags":[],"pageTags":[]}\n' \
  '/Briefs/i' '-'

case_run_with_processed() {
  local name="$1" state="$2" stage1="$3" fetch="$4" processed="$5" want_fire="$6" want_log="$7"
  printf '%b' "$state"  > "$WORK/state.tsv"
  printf '%b' "$stage1" > "$WORK/stage1.tsv"
  printf '%b' "$fetch"  > "$WORK/fetch.tsv"
  printf '%b' "$processed" > "$WORK/processed.tsv"
  : > "$WORK/log.txt"
  local fired logged ok=1
  fired=$(STATE="$WORK/state.tsv" PROCESSED_STATE="$WORK/processed.tsv" LOG="$WORK/log.txt" \
          TAG_FLAGS="${TAG_FLAGS:-$TAG_FLAGS_SRC}" \
          FIXTURE_STAGE1="$WORK/stage1.tsv" FIXTURE_FETCH="$WORK/fetch.tsv" \
          bash "$WORK/harness.sh" 2>&1)
  logged=$(cat "$WORK/log.txt")
  [ "$fired" = "$want_fire" ] || ok=0
  if [ "$want_log" = "-" ]; then [ -z "$logged" ] || ok=0
  else printf '%s' "$logged" | grep -q "$want_log" || ok=0; fi
  if [ "$ok" = 1 ]; then echo "  [ ok ] $name"; else
    note_fail "$name"; echo "         fired want=[$want_fire] got=[$fired]"
    echo "         log   want=[$want_log] got=[$logged]"; fi
}

case_run_with_processed "processed ready doc with unchanged ink hash stays quiet (mc-ld7g4)" \
  '/Briefs/decisions-2026-10-05\tM1\tH0\ty\tn\n' \
  '/Briefs/decisions-2026-10-05\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/decisions-2026-10-05\tH1\t{"tags":[],"pageTags":[]}\n' \
  '/Briefs/decisions-2026-10-05\tH1\t2026-10-05T14:09:50-0700\n' \
  '' 'quiet: already processed ink hash'

case_run_with_processed "processed ready doc with new ink hash still fires (mc-ld7g4)" \
  '/Briefs/decisions-2026-10-05\tM1\tH1\ty\tn\n' \
  '/Briefs/decisions-2026-10-05\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/decisions-2026-10-05\tH2\t{"tags":[],"pageTags":[]}\n' \
  '/Briefs/decisions-2026-10-05\tH1\t2026-10-05T14:09:50-0700\n' \
  '/Briefs/decisions-2026-10-05' '-'

case_run "no tags, ink change stays quiet" \
  '/Briefs/e\tM1\tH1\tn\tn\n' \
  '/Briefs/e\t{"ModifiedClient":"M2","Tags":[]}\n' \
  '/Briefs/e\tH2\t{"tags":[],"pageTags":[]}\n' \
  '' 'quiet: ink changed'

case_run "ready on a verifiably ink-free doc does not fire" \
  '/Briefs/f\tM1\tH1\tn\tn\n' \
  '/Briefs/f\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/f\tnone\t{"tags":[],"pageTags":[]}\n' \
  '' '-'

# Migration: unchanged metadata adopts current device state and says nothing.
# A receipt means "he just finished reading this"; backdating one would lie.
case_run "legacy 4-field row migrates silently, no backdated receipt" \
  '/Briefs/g\tM1\tH1\tn\n' \
  '/Briefs/g\t{"ModifiedClient":"M1","Tags":["Mark-as-Read"]}\n' \
  '/Briefs/g\tH1\t{"tags":[],"pageTags":[]}\n' \
  '' '-'
if [ "$(awk -F'\t' 'NR==1{print NF}' "$WORK/state.tsv")" = "5" ] \
   && [ "$(awk -F'\t' 'NR==1{print $5}' "$WORK/state.tsv")" = "y" ]; then
  echo "  [ ok ] migrated row carries 5 fields and adopted the device tag state"
else
  note_fail "migration produced: $(cat "$WORK/state.tsv")"
fi

# A document we cannot read must not lose its history. This is the state-level
# consequence of the R3 blocker: the row survives, nothing fires, nothing is
# logged, and the baseline does not advance.
case_run "unreadable stat payload preserves the existing row and fires nothing" \
  '/Briefs/u\tM1\tH1\ty\ty\n' \
  '/Briefs/u\t[]\n' \
  '/Briefs/u\tH2\t{"tags":[],"pageTags":[]}\n' \
  '' '-'
if [ "$(cat "$WORK/state.tsv")" = "$(printf '/Briefs/u\tM1\tH1\ty\ty')" ]; then
  echo "  [ ok ] the preserved row is byte-identical to the stored one"
else
  note_fail "row not preserved verbatim: [$(cat "$WORK/state.tsv")]"
fi

case_run "unreadable stat for a never-seen doc invents no baseline" \
  '' \
  '/Briefs/v\tnull\n' \
  '/Briefs/v\tH1\t{"tags":[],"pageTags":[]}\n' \
  '' '-'
if [ ! -s "$WORK/state.tsv" ]; then
  echo "  [ ok ] no row invented from a failed read"
else
  note_fail "invented a baseline from an unreadable payload: [$(cat "$WORK/state.tsv")]"
fi

# mc-110b7 (the reader 2026-09-07): Previous Briefs is never a trigger, no matter what
# changes on it. Moving the document out of the archive is the only signal.
case_run "archived path with new ink and ready tag does not fire (mc-110b7)" \
  '/Briefs/Previous Briefs/x\tM1\tH1\ty\tn\n' \
  '/Briefs/Previous Briefs/x\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/Previous Briefs/x\tH2\t{"tags":[],"pageTags":[]}\n' \
  '' 'quiet: archived, ignored by rule mc-110b7'

case_run "same document moved back to /Briefs with the tag fires once (mc-110b7)" \
  '/Briefs/Previous Briefs/x\tM1\tH1\ty\tn\n' \
  '/Briefs/x\t{"ModifiedClient":"M3","Tags":["ready"]}\n' \
  '/Briefs/x\tH3\t{"tags":[],"pageTags":[]}\n' \
  '/Briefs/x' '-'

case_run "untagged move out of the archive stays quiet, no fire (mc-110b7)" \
  '/Briefs/Previous Briefs/y\tM1\tH1\tn\tn\n' \
  '/Briefs/y\t{"ModifiedClient":"M4","Tags":[]}\n' \
  '/Briefs/y\tH4\t{"tags":[],"pageTags":[]}\n' \
  '' 'quiet: new doc with ink, no ready tag'

case_run "--baseline still scans archived paths but never fires them (mc-110b7)" \
  '/Briefs/Previous Briefs/z\tM1\tH1\ty\tn\n' \
  '/Briefs/Previous Briefs/z\t{"ModifiedClient":"M2","Tags":["ready"]}\n' \
  '/Briefs/Previous Briefs/z\tH2\t{"tags":[],"pageTags":[]}\n' \
  '' '-' --baseline
if [ "$(cat "$WORK/state.tsv")" = "$(printf '/Briefs/Previous Briefs/z\tM2\tH2\ty\tn')" ]; then
  echo "  [ ok ] --baseline still updates the archived row's hash and tag, no fire"
else
  note_fail "baseline archived row not updated as expected: [$(cat "$WORK/state.tsv")]"
fi

# --inner is the mutation control re-entering this file. It must stop HERE,
# before Section 3, or the control spawns itself forever.
case "${1:-}" in
  --inner) exit "$([ "$FAILURES" = 0 ] && echo 0 || echo 1)" ;;
esac

# ---------------------------------------------------------------------------
# Section 3: mutation control.
# ---------------------------------------------------------------------------
# The control the original suite lacked. Break the production matcher and this
# file must fail. If this section reports that a broken matcher still passes,
# the tests above are decorative and must not be trusted.
echo "mutation control:"
sed 's/^SEEN = frozenset({"markasread", "markedasread"})$/SEEN = frozenset({"nevermatch"})/' "$TAG_FLAGS_SRC" > "$WORK/tag_flags_mutated.py"
if ! grep -q 'SEEN = frozenset({"nevermatch"})' "$WORK/tag_flags_mutated.py"; then
  note_fail "could not construct the mutant (SEEN constant not found)"
else
  mut_out=$(TAG_FLAGS="$WORK/tag_flags_mutated.py" "$0" --inner 2>&1)
  if printf '%s' "$mut_out" | grep -q '\[FAIL\]'; then
    echo "  [ ok ] breaking the mark-as-read matcher makes this suite fail"
  else
    note_fail "MUTANT SURVIVED: the suite passes with the matcher broken"
  fi
fi

echo
if [ "$FAILURES" = 0 ]; then
  echo "PASS: production parsers exercised, state machine holds, mutant dies"
  exit 0
fi
echo "FAIL: $FAILURES case(s) regressed"
exit 1
