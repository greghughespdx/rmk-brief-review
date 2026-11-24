#!/usr/bin/env bash
# The Decisions document loop (mc-j5dr4, the reader 2026-09-07, his words):
#
#   "I want decisions documents processed, and if there are still cards or
#   questions remaining, I want a new decisions document created and pushed to
#   the reMarkable and the processed decisions doc filed. ... what I really
#   want is that document updated and replaced with a document that contains
#   the decisions that still remain."
#
# The pieces already existed and nothing joined them up. This is the join, and
# it is deliberately deterministic: it runs the gates and the mechanics, and it
# STOPS and reports wherever the reader's design puts a model. Two of those:
#
#   the ANSWERING step - a model answers every question and clarification he
#   penned, with the card's full context, before the follow-up renders;
#   the REVIEW step - an independent reviewer reads the rendered document and
#   writes the hash-bound receipt.
#
# The reader 2026-09-07, on removing either: "I am completely in 101% unwilling to go
# backwards in that regard. So, removing the Agent from the process would be a
# horrific mistake." So this script never answers and never reviews. With
# either missing it exits nonzero and says exactly what is needed. It never
# pushes an unreviewed document; the receipt gate inside push-decisions.sh and
# replace-decisions-document.sh is the enforcement, and this script reaches it
# rather than routing around it.
#
# Usage:
#   decisions-loop.sh status  --date YYYY-MM-DD
#   decisions-loop.sh render  --date YYYY-MM-DD
#   decisions-loop.sh deliver --date YYYY-MM-DD [--apply]
#   decisions-loop.sh file-processed --date YYYY-MM-DD
#
# Any command also takes --dry-run [--fixture-dir DIR]. In a dry run the real
# tablet is never contacted: rmapi is replaced by a stub that acts on a fixture
# directory tree and logs every call, and deliver stops after the receipt gate
# instead of pushing. The gates themselves are the real gates, so a dry run
# that reaches "would push" is one whose receipt really did match the bytes.
#
# render and deliver are two commands on purpose. The receipt is bound to the
# PDF's hash, so a re-render between the review and the push voids the review
# (the 2026-09-13 and 2026-09-14 crossings). render stops with the document
# rendered and unreviewed; the reviewer writes the receipt; deliver sends the
# reviewed bytes and nothing else.
#
# Exit codes:
#   0  the step finished
#   2  usage
#   4  review gate: no receipt matching these exact bytes; nothing was pushed
#  11  the document on the device has not been consumed yet
#  12  answer gate: penned questions still need a model's answer
#  13  nothing remains: the re-render produced no cards
#  14  file-processed refused: cards still remain, could not be counted, or
#      the empty manifest is not the zero-card render the loop performed
#  15  file-processed partial: the document was filed, the registry was not
#   anything else is push-decisions.sh's own code, passed through

set -uo pipefail

DIR_RMW="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Load the config into RMW_* variables. A copy of this script outside the
# repository (a test harness) falls back to RMW_PROJECT_ROOT, then to the
# built-in defaults below.
if [ -f "$DIR_RMW/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$DIR_RMW/rmw-env.sh"
elif [ -n "${RMW_PROJECT_ROOT:-}" ] && [ -f "$RMW_PROJECT_ROOT/remarkable/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$RMW_PROJECT_ROOT/remarkable/rmw-env.sh"
fi
# The same defaults rmw_config.py uses, for a run with no config loaded.
: "${RMW_PATHS_HOME:=$HOME/.local/share/remarkable-workflow}"
: "${RMW_ARCHIVE:=$RMW_PATHS_HOME/archive}"
: "${RMW_DECISIONS_ARCHIVE:=$RMW_ARCHIVE/decisions}"
: "${RMW_PATHS_QUEUE:=$RMW_PATHS_HOME/decisions-queue.md}"
: "${RMW_PATHS_REGISTRY:=$HOME/.remarkable-today-pages.json}"
: "${RMW_RENDER_PYTHON:=python3}"
: "${RMW_WRITER_IDENTITY:=writer}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MC="$(cd "$DIR/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
PUSH_DECISIONS="${DECISIONS_PUSH_DECISIONS:-$DIR/push-decisions.sh}"
VERIFY_QUEUE="${DECISIONS_VERIFY_QUEUE:-$DIR/verify-queue.py}"
RECEIPT_TOOL="${DECISIONS_RECEIPT_TOOL:-$DIR/review-receipt.py}"
RMAPI="${RMAPI:-$DIR/rmapi-retry.sh}"
ARCHIVE_ROOT="${REMARKABLE_TODAY_ARCHIVE:-$RMW_DECISIONS_ARCHIVE}"
QUESTION_STATE="${REMARKABLE_QUESTION_STATE:-$RMW_ARCHIVE/question-state.json}"
REGISTRY="${REMARKABLE_TODAY_REGISTRY:-$RMW_PATHS_REGISTRY}"
BRIEFS="${DECISIONS_BRIEFS:-${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}}"
PREV_FOLDER="${DECISIONS_PREV_FOLDER:-${RMW_TABLET_PREVIOUS_FOLDER:-$BRIEFS/Previous Briefs}}"
DRY_RUN=0
FIXTURE_DIR=""

COMMAND="${1:-}"
[ -n "$COMMAND" ] && shift
DATE=""
APPLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --date) DATE="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --briefs) BRIEFS="$2"; PREV_FOLDER="$2/Previous Briefs"; shift 2 ;;
    --prev-folder) PREV_FOLDER="$2"; shift 2 ;;
    --archive-root) ARCHIVE_ROOT="$2"; shift 2 ;;
    --question-state) QUESTION_STATE="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --fixture-dir) FIXTURE_DIR="$2"; DRY_RUN=1; shift 2 ;;
    *) echo "decisions-loop.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$COMMAND" in
  status|render|deliver|file-processed) : ;;
  *) echo "usage: decisions-loop.sh <status|render|deliver|file-processed> --date YYYY-MM-DD" >&2; exit 2 ;;
esac
if ! printf '%s' "$DATE" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "decisions-loop.sh: --date YYYY-MM-DD is required" >&2
  exit 2
fi

NAME="decisions-$DATE"
OUT_DIR="$ARCHIVE_ROOT/$DATE"
PDF="$OUT_DIR/$NAME.pdf"
MANIFEST="$OUT_DIR/$NAME.manifest.json"
RECEIPT="$OUT_DIR/review-receipt.json"
PROCESSED="$OUT_DIR/processed"
RESOLUTION="$PROCESSED/resolution.json"
ANSWERS="$OUT_DIR/answers-complete.json"
LOOP_STATE="$OUT_DIR/loop-state.json"
DEVICE_PATH="$BRIEFS/$NAME"

# ---------------------------------------------------------------------------
# Dry run: the gates are real, the device is not.
#
# The tablet is the reader's own device, so there has to be a way to exercise this
# whole loop without touching it. --dry-run swaps rmapi for a stub over a
# fixture directory tree and logs every call it is asked to make, and stops
# deliver after the receipt gate rather than pushing. Nothing here weakens a
# gate: a dry run that reports "would push" is one where the receipt really
# did match the bytes, and a dry run that exits 4 would have refused for real.
# ---------------------------------------------------------------------------
DRY_LOG=""
setup_dry_run() {
  FIXTURE_DIR="${FIXTURE_DIR:-$OUT_DIR/dry-run}"
  DRY_LOG="$FIXTURE_DIR/calls.log"
  mkdir -p "$FIXTURE_DIR/bin" "$FIXTURE_DIR/device$BRIEFS" \
           "$FIXTURE_DIR/device$PREV_FOLDER" || return 1
  : >> "$DRY_LOG"
  # A fixture with no document on it would make file-processed fail for a
  # reason the real device would not have, so one is seeded when absent.
  if [ ! -e "$FIXTURE_DIR/device$DEVICE_PATH" ]; then
    printf 'fixture stand-in for the Decisions document of %s\n' "$DATE" \
      > "$FIXTURE_DIR/device$DEVICE_PATH"
  fi
  cat > "$FIXTURE_DIR/bin/rmapi" <<'STUB'
#!/usr/bin/env bash
# Fixture rmapi for decisions-loop.sh --dry-run. It never reaches the cloud.
set -uo pipefail
root="$DRY_DEVICE"
log="$DRY_LOG"
printf '%s\n' "$*" >> "$log"
cmd="${1:-}"; shift || true
case "$cmd" in
  ls)
    dir="$root/${1#/}"
    [ -d "$dir" ] || exit 0
    for item in "$dir"/*; do
      [ -e "$item" ] || continue
      if [ -d "$item" ]; then printf '[d]\t%s\n' "$(basename "$item")"
      else printf '[f]\t%s\n' "$(basename "$item")"; fi
    done
    ;;
  stat)
    target="$root/${1#/}"
    [ -e "$target" ] || exit 1
    printf '{"ModifiedClient":"dry-run","Tags":[]}\n'
    ;;
  mkdir) mkdir -p "$root/${1#/}" ;;
  mv)
    # DRY_MV_FAILS lets a test exercise the filing-failed path, which is the
    # one that must never fall back to deleting anything.
    [ -n "${DRY_MV_FAILS:-}" ] && { echo "move refused" >&2; exit 1; }
    src="$root/${1#/}"; dst="$root/${2#/}"
    [ -e "$src" ] || { echo "no such document: $1" >&2; exit 1; }
    [ -d "$dst" ] || { echo "no such folder: $2" >&2; exit 1; }
    mv "$src" "$dst/"
    ;;
  rm)
    echo "DRY RUN REFUSES rm: $*" >&2
    exit 1
    ;;
  get|put|geta) : ;;
  *) exit 2 ;;
esac
STUB
  chmod +x "$FIXTURE_DIR/bin/rmapi" || return 1
  export DRY_DEVICE="$FIXTURE_DIR/device" DRY_LOG
  RMAPI="$FIXTURE_DIR/bin/rmapi"
  echo "DRY RUN: rmapi is the fixture stub over $FIXTURE_DIR/device; calls log to $DRY_LOG"
  return 0
}
if [ "$DRY_RUN" -eq 1 ]; then
  setup_dry_run || { echo "decisions-loop.sh: could not set up the dry-run fixture" >&2; exit 1; }
fi

json_get() {  # json_get <file> <python expression over `doc`>
  "$PYTHON" - "$1" "$2" <<'PY' 2>/dev/null
import json, pathlib, sys
try:
    doc = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
try:
    print(eval(sys.argv[2], {"doc": doc, "len": len, "sorted": sorted,
                             "isinstance": isinstance, "list": list}))
except Exception:
    raise SystemExit(1)
PY
}

# ---------------------------------------------------------------------------
# The identity of the document the reader answered, kept APART from the manifest.
#
# render replaces the manifest in place, and today_page.py derives page_id from
# the content and the source hashes (today_page.py, "page_id = f\"{document_name}
# -{sha256_bytes(canonical_json(seed)...)}\""), so every render of a different
# set of cards is a different page. Observed with the real builder for
# 2026-08-20: the original two-card document is decisions-2026-08-20-903e1b3000ef,
# the one-card follow-up is ...-ba75a97b0db0, and the zero-card render is
# ...-202c0dc5d9c6.
#
# So the consume gate cannot be asked about the CURRENT manifest once a render
# has happened: file-processed runs exactly there, and it would compare the reader's
# resolution of the original page against the zero-card render's page and exit
# 11 on its own intended path (the cross-reviewer review, mc-j5dr4). render therefore
# writes down what it consumed BEFORE it supersedes anything, and records the
# zero-card render's own id when the re-render comes back empty. Then:
#
#   the consume gate checks the resolution against the CONSUMED id;
#   the remaining gate checks the manifest against the ZERO-CARD id.
#
# Two separate bindings, each to the thing it is actually proving.
# ---------------------------------------------------------------------------
LOOP_STATE_TOOL="rmw-decisions-loop-v1"

loop_state_get() {  # loop_state_get <key>
  json_get "$LOOP_STATE" \
    "doc.get(\"$1\",\"\") if doc.get(\"tool\") == \"$LOOP_STATE_TOOL\" else \"\""
}

loop_state_set() {  # loop_state_set <key> <value>
  "$PYTHON" - "$LOOP_STATE" "$LOOP_STATE_TOOL" "$1" "$2" <<'PY'
import json, os, pathlib, sys, tempfile
path, tool, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
try:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("tool") != tool:
        doc = {}
except Exception:
    doc = {}
doc["tool"] = tool
doc[key] = value
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, path)
PY
}

manifest_page_id() { json_get "$MANIFEST" 'doc.get("page_id","")'; }

# The registry records the page on the tablet, independently of local renders.
# Without a registered page id, keep the original current-manifest check.
render_source_page() {
  local registered current
  registered=""
  if [ -e "$REGISTRY" ]; then
    registered="$(json_get "$REGISTRY" 'doc.get("pages",{}).get("'"$DEVICE_PATH"'",{}).get("page_id","")')" || {
      echo "The tablet's document record could not be read. Nothing was rendered." >&2
      return 1
    }
  fi
  current="$(manifest_page_id)"
  if [ -n "$registered" ]; then
    printf '%s\n' "$registered"
  else
    printf '%s\n' "$current"
  fi
}

consumed_manifest_path() {
  local page="$1" source
  # The page id becomes a filename, so refuse path components before using it.
  [[ "$page" =~ ^[a-zA-Z0-9_-]+$ ]] || return 1
  source="$MANIFEST"
  if [ "$(manifest_page_id)" != "$page" ]; then
    source="$OUT_DIR/consumed-$page.manifest.json"
  fi
  [ "$(json_get "$source" 'doc.get("page_id","")')" = "$page" ] || return 1
  printf '%s\n' "$source"
}

preserve_consumed_manifest() {
  local source="$1" target="$OUT_DIR/consumed-$2.manifest.json"
  if [ -f "$target" ]; then
    cmp -s "$source" "$target" || {
      echo "The saved answered document differs from its source. Nothing was rendered." >&2
      return 1
    }
  else
    cp -p "$source" "$target" || return 1
  fi
}

# ---------------------------------------------------------------------------
# Gate 1: has the document now on the device actually been consumed?
#
# The loop starts when the reader tags a document ready and the watcher's consumer
# records his answers. Re-rendering before that would print questions he has
# already answered, which is the failure the whole chain exists to prevent, so
# the resolution has to exist AND be a resolution of THIS document: the page
# id in it must match the page id of the manifest that was pushed.
# ---------------------------------------------------------------------------
consume_gate() {  # consume_gate [<page id the resolution must match>]
  local expected_page="${1:-}"
  if [ ! -f "$RESOLUTION" ]; then
    echo "NOT CONSUMED: there is no $RESOLUTION." >&2
    echo "The document on the device has not been processed yet, so a" >&2
    echo "follow-up would re-ask questions the reader may already have answered." >&2
    echo "The watcher runs the consumer when he tags the document ready." >&2
    return 11
  fi
  local resolved_page manifest_page
  resolved_page="$(json_get "$RESOLUTION" 'doc.get("page_id","")')"
  # With no id passed in, the document in question is the one the manifest
  # currently describes - which is the right question BEFORE a re-render.
  manifest_page="$expected_page"
  [ -n "$manifest_page" ] || manifest_page="$(manifest_page_id)"
  if [ -z "$resolved_page" ] || [ -z "$manifest_page" ]; then
    echo "NOT CONSUMED: could not read a page id from the resolution or the manifest." >&2
    return 11
  fi
  if [ "$resolved_page" != "$manifest_page" ]; then
    echo "NOT CONSUMED: $RESOLUTION is a resolution of page $resolved_page," >&2
    echo "but the document for $DATE is page $manifest_page. That resolution" >&2
    echo "belongs to a different document; nothing has been re-rendered." >&2
    return 11
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Gate 2: has a model answered what the reader penned?
#
# The reader 2026-09-07: "if I pen a question or a comment or ask for a clarification
# then when that decisions document is processed, I would hope that that
# question or request would be dealt with right away and a new document loaded
# onto the reMarkable so that I can look at it with the new context attached."
#
# Penned work includes clarification questions, write-ins, notes, marks
# with no question attached, and ink on pages outside the rendered layout. The answering agent records what it dealt with in
# answers-complete.json and the gate passes only when that record covers all of
# it.
# ---------------------------------------------------------------------------
pending_answers() {
  "$PYTHON" - "$QUESTION_STATE" "${1:-$MANIFEST}" "$PROCESSED" "$ANSWERS" <<'PY'
import json, pathlib, sys

# Every failure in here is an ERROR, printed on stderr, exiting non-zero.
#
# It used to swallow them. A record carrying "handled": null raised inside the
# set comprehension, the traceback went to stderr, stdout stayed empty - and
# the caller read that empty stdout as "nothing outstanding" and opened the
# gate (the cross-reviewer review, mc-j5dr4). An unreadable input is never evidence that
# The reader's penned questions have been answered, so it stops the loop instead.

state_path, manifest_path, processed, answers_path = (pathlib.Path(p) for p in sys.argv[1:5])


def fail(message):
    print(f"decisions-loop: {message}", file=sys.stderr)
    raise SystemExit(3)


def load(path, default, label):
    # Absent is allowed and means "nothing recorded"; present-but-unreadable
    # is not, because it is indistinguishable from a record that would have
    # held outstanding work.
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"{label} at {path} could not be parsed: {exc}")


manifest = load(manifest_path, {}, "the manifest")
if not isinstance(manifest, dict):
    fail(f"the manifest at {manifest_path} is not a JSON object")
page_id = manifest.get("page_id", "")

state = load(state_path, {}, "the question state")
if not isinstance(state, dict):
    fail(f"the question state at {state_path} is not a JSON object")
entries = state.get("entries") or {}
if not isinstance(entries, dict):
    fail(f"the question state at {state_path} has an 'entries' that is not an object")

def page_evidence(filename):
    artifact = load(processed / filename, {}, filename)
    if not isinstance(artifact, dict):
        fail(f"{filename} is not a JSON object")
    return artifact if page_id and artifact.get("page_id") == page_id else {}


pending = []
for entry_id, record in entries.items():
    if not isinstance(record, dict):
        continue
    if record.get("state") == "needs_clarification" and record.get("source") == page_id:
        pending.append(f"clarification:{entry_id}")

ambiguities = page_evidence("resolution.json").get("ambiguities", [])
if not isinstance(ambiguities, list) or any(not isinstance(mark, dict) for mark in ambiguities):
    fail("the resolution has invalid ambiguity records")
for mark in ambiguities:
    if mark.get("entry_id") or mark.get("candidate_entry_ids"):
        continue
    stroke_id = mark.get("stroke_id")
    if not isinstance(stroke_id, str) or not stroke_id:
        fail("an ambiguous mark has no stroke id")
    pending.append(f"ambiguity:{stroke_id}")

stroke_source = page_evidence("strokes.json").get("source", {})
if not isinstance(stroke_source, dict):
    fail("the strokes file has no valid source record")
unplaced_pages = stroke_source.get("unplaced_pages", [])
if not isinstance(unplaced_pages, list) or any(not isinstance(page, dict) for page in unplaced_pages):
    fail("the strokes file has invalid unplaced page records")
for page in unplaced_pages:
    line_count = page.get("line_count")
    if type(line_count) is not int or line_count < 0:
        fail("an unplaced page has no valid line count")
    if line_count:
        device_page_id = page.get("rm_page_id")
        if not isinstance(device_page_id, str) or not device_page_id:
            fail("an unplaced inked page has no page id")
        pending.append(f"unplaced-page:{device_page_id}")

for folder in ("write-ins", "notes"):
    directory = processed / folder
    if not directory.is_dir():
        continue
    for item in sorted(directory.iterdir()):
        if item.is_file() and item.suffix.lower() == ".png":
            pending.append(f"{folder}:{item.name}")

answers = load(answers_path, None, "the answer record")
covered = set()
if isinstance(answers, dict) and answers.get("tool") == "rmw-decisions-answers-v1":
    if answers.get("page_id") == page_id:
        handled = answers.get("handled")
        if not isinstance(handled, list):
            fail(
                f"the answer record at {answers_path} has a 'handled' that is "
                f"{type(handled).__name__}, not a list of the items it answered"
            )
        covered = {str(item) for item in handled}
elif answers is not None and not isinstance(answers, dict):
    fail(f"the answer record at {answers_path} is not a JSON object")

outstanding = [item for item in pending if item not in covered]
for item in outstanding:
    print(item)
PY
}

answer_gate() {
  local outstanding status
  # The substitution's status is the check's status. Without this line an
  # unreadable answer record printed a traceback, produced no stdout, and the
  # gate read that as "nothing outstanding" and let the render through.
  outstanding="$(pending_answers "${1:-$MANIFEST}")"
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "ANSWER GATE: the penned-work check did not complete (exit $status)." >&2
    echo "The line above says which file could not be read. Until that check" >&2
    echo "runs to completion there is no evidence that the reader's penned questions" >&2
    echo "have been answered, so the gate refuses. Nothing was re-rendered." >&2
    return 12
  fi
  [ -z "$outstanding" ] && return 0
  echo "ANSWER GATE: the reader penned work on $NAME that no model has answered yet:" >&2
  printf '%s\n' "$outstanding" | sed 's/^/  /' >&2
  echo "" >&2
  echo "A model reads each one with the card's full context and answers it," >&2
  echo "then records what it handled in:" >&2
  echo "  $ANSWERS" >&2
  echo 'Shape: {"tool":"rmw-decisions-answers-v1","page_id":"<id>",' >&2
  echo '        "answered_by":"<agent>","answered_at":"<iso8601>","handled":[...]}' >&2
  echo "with every line above listed in handled. Nothing was re-rendered." >&2
  return 12
}

# How many cards the manifest holds. It prints NOTHING unless "cards" is
# actually a list: len() of a string counts its characters, so a manifest
# carrying "cards":"lots" used to report four cards, and a manifest carrying
# "cards":4 used to report nothing at all. remaining_gate treats anything that
# is not a plain number as unproven, so the distinction has to be made here.
card_count() {
  json_get "$MANIFEST" 'len(doc["cards"]) if isinstance(doc.get("cards"), list) else ""'
}

supersede_current() {
  local stamp target
  stamp="$(date +%Y%m%d-%H%M%S)"
  target="$OUT_DIR/superseded-$stamp"
  mkdir -p "$target" || return 1
  local moved=0 file
  for file in "$PDF" "$MANIFEST" "$RECEIPT"; do
    [ -f "$file" ] || continue
    cp -p "$file" "$target/" || return 1
    moved=$((moved + 1))
  done
  # The receipt is moved out, not copied: it belongs to the bytes that are
  # being replaced, and leaving it beside a fresh render is how an unreviewed
  # document reaches the device wearing a receipt written for another one.
  rm -f "$RECEIPT"
  echo "superseded copies kept in $target ($moved file(s)); the old receipt is retired"
}

cmd_status() {
  echo "Decisions loop status for $DATE"
  echo "  document:   $PDF $([ -f "$PDF" ] && echo present || echo MISSING)"
  echo "  cards:      $(card_count 2>/dev/null || echo '?')"
  local page source_manifest outstanding status
  page="$(render_source_page)" || return 11
  # Once filed, the registered page is gone. Keep reporting the answered
  # page when the current manifest is only a local follow-up render.
  if [ "$page" = "$(manifest_page_id)" ] && [ "$page" = "$(loop_state_get zero_card_page_id)" ]; then
    page="$(loop_state_get consumed_page_id)"
  fi
  echo "  consumed:   $(consume_gate "$page" >/dev/null 2>&1 && echo yes || echo no)"
  if [ -z "$page" ]; then
    outstanding="$(pending_answers)"
    status=$?
  elif source_manifest="$(consumed_manifest_path "$page")"; then
    outstanding="$(pending_answers "$source_manifest")"
    status=$?
  else
    outstanding=""
    status=11
    echo "The answered document's saved manifest is missing." >&2
  fi
  if [ "$status" -ne 0 ]; then
    echo "  answers:    UNKNOWN - the penned-work check failed (exit $status); see above"
  elif [ -z "$outstanding" ]; then
    echo "  answers:    nothing penned is waiting on a model"
  else
    echo "  answers:    $(printf '%s\n' "$outstanding" | wc -l | tr -d ' ') item(s) waiting on a model"
    printf '%s\n' "$outstanding" | sed 's/^/                /'
  fi
  if "$PYTHON" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check --date "$DATE" \
      --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
      --producer "${REMARKABLE_DOC_PRODUCER:-$RMW_WRITER_IDENTITY}" >/dev/null 2>&1; then
    echo "  receipt:    valid for these exact bytes; deliver will push"
  else
    echo "  receipt:    NOT valid for these bytes; deliver will refuse to push"
  fi
  return 0
}

cmd_render() {
  local consumed_page source_manifest
  consumed_page="$(render_source_page)" || return 11
  consume_gate "$consumed_page" || return $?
  source_manifest="$(consumed_manifest_path "$consumed_page")" || {
    echo "The answered document's saved manifest is missing. Nothing was rendered." >&2
    return 11
  }
  answer_gate "$source_manifest" || return $?
  preserve_consumed_manifest "$source_manifest" "$consumed_page" || return 1
  supersede_current || { echo "could not archive the superseded document" >&2; return 1; }
  loop_state_set consumed_page_id "$consumed_page"
  # Any zero-card proof on file belongs to an earlier render, not this one.
  loop_state_set zero_card_page_id ""
  local queue report
  queue="$OUT_DIR/verified-queue-remaining.md"
  report="$OUT_DIR/verify-remaining.json"
  if ! "$PYTHON" "$VERIFY_QUEUE" --out-queue "$queue" --report "$report" ${PHONE_CARDS_URL:+--live-cards "$PHONE_CARDS_URL"}; then
    echo "The remaining rows could not be verified current; nothing was rendered." >&2
    return 3
  fi
  local code
  "$PUSH_DECISIONS" --date "$DATE" --queue "$queue" --verify-report "$report"
  code=$?
  if [ "$code" -ne 0 ] && [ "$code" -ne 5 ]; then
    echo "The follow-up document did not render (push-decisions.sh exit $code)." >&2
    return "$code"
  fi
  local cards
  cards="$(card_count)"
  if ! [[ "$cards" =~ ^[0-9]+$ ]] || { [ "$code" -eq 5 ] && [ "$cards" != 0 ]; }; then
    echo "The follow-up render did not produce a valid card count." >&2
    return 2
  fi
  if [ "$cards" = 0 ]; then
    local zero_page
    zero_page="$(manifest_page_id)"
    if ! [[ "$zero_page" =~ ^[a-zA-Z0-9_-]+$ ]] || [ "$zero_page" = "$consumed_page" ]; then
      echo "The empty follow-up render did not identify a separate page." >&2
      return 2
    fi
    loop_state_set zero_card_page_id "$zero_page" || return 1
    echo "NOTHING REMAINS: no card survived for $DATE." >&2
    echo "The reader has settled or deferred everything that was open. Do not push a" >&2
    echo "blank document; file the processed one instead:" >&2
    echo "  $DIR/decisions-loop.sh file-processed --date $DATE" >&2
    return 13
  fi
  echo "Rendered $cards remaining card(s) into $PDF."
  echo "It is UNREVIEWED. An independent reviewer (not the producer) reads it and writes"
  echo "the receipt, then:"
  echo "  $DIR/decisions-loop.sh deliver --date $DATE"
  return 0
}

cmd_deliver() {
  if [ ! -f "$PDF" ] || [ ! -f "$MANIFEST" ]; then
    echo "There is no rendered document for $DATE at $OUT_DIR." >&2
    return 2
  fi
  if ! "$PYTHON" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check --date "$DATE" \
      --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
      --producer "${REMARKABLE_DOC_PRODUCER:-$RMW_WRITER_IDENTITY}"; then
    echo "" >&2
    echo "REVIEW GATE: the follow-up document for $DATE has no receipt matching" >&2
    echo "these exact bytes. A fresh render needs a fresh review. Nothing was" >&2
    echo "pushed and the document on the device is untouched." >&2
    return 4
  fi
  local args=(--date "$DATE" --no-generate --push)
  [ "$APPLY" -eq 1 ] && args+=(--apply)
  if [ "$DRY_RUN" -eq 1 ]; then
    # The receipt gate above already ran for real and passed, so this is the
    # point where the live loop would send the reviewed bytes.
    printf 'push-decisions %s\n' "${args[*]}" >> "$DRY_LOG"
    echo "DRY RUN: would push the reviewed $NAME with: $PUSH_DECISIONS ${args[*]}"
    echo "DRY RUN: nothing was sent to the tablet."
    return 0
  fi
  "$PUSH_DECISIONS" "${args[@]}"
}

# ---------------------------------------------------------------------------
# Gate 3: does nothing actually remain to ask?
#
# file-processed is the END of the loop. It is reached only when the re-render
# produced no cards, which is the exit-13 path in cmd_render. The consume gate
# alone does NOT establish that: a document can be consumed with plenty of
# cards still open, and filing it then takes the reader's open questions off the
# device with no replacement behind them (the cross-reviewer review, mc-j5dr4).
#
# The bound proof is the manifest of the zero-card RE-RENDER. It is a different
# page from the document the reader answered - today_page derives page_id from the
# content and the source hashes - so this gate cannot lean on consume_gate's
# binding; it does its own, against the page id render recorded when it exited
# 13. A manifest that is missing, unparseable, carries a card count that is not
# a number, or is a zero from some other render is NOT a proof, and does not
# open the gate.
# ---------------------------------------------------------------------------
remaining_gate() {
  local cards
  cards="$(card_count)"
  if ! printf '%s' "$cards" | grep -Eq '^[0-9]+$'; then
    echo "NOT PROVEN EMPTY: could not read a card count from $MANIFEST." >&2
    echo "file-processed takes the document off the device for good, so it" >&2
    echo "runs only on a manifest that PROVES nothing remains. Nothing was" >&2
    echo "moved and the registry is untouched." >&2
    return 14
  fi
  if [ "$cards" = "0" ]; then
    # A zero on its own is not the proof. The manifest is a file on disk that
    # anything could have written, so the zero has to be the zero of the
    # re-render this loop performed: same page id the zero-card render
    # produced. render records that id at the moment it exits 13.
    local expected_page actual_page
    expected_page="$(loop_state_get zero_card_page_id)"
    actual_page="$(manifest_page_id)"
    if [ -z "$expected_page" ]; then
      echo "NOT PROVEN EMPTY: no zero-card render is on record for $DATE." >&2
      echo "file-processed takes the document off the device for good, so the" >&2
      echo "empty manifest has to be one this loop rendered. Run:" >&2
      echo "  $DIR/decisions-loop.sh render --date $DATE" >&2
      echo "and file only when it exits 13 with NOTHING REMAINS. Nothing was" >&2
      echo "moved and the registry is untouched." >&2
      return 14
    fi
    if [ "$actual_page" != "$expected_page" ]; then
      echo "NOT PROVEN EMPTY: $MANIFEST is page $actual_page, but the zero-card" >&2
      echo "render for $DATE produced page $expected_page. This empty manifest is" >&2
      echo "not the one the loop rendered, so it proves nothing about what is" >&2
      echo "still open. Nothing was moved and the registry is untouched." >&2
      return 14
    fi
    return 0
  fi
  if [ "$cards" != "0" ]; then
    echo "STILL OPEN: $cards card(s) in $MANIFEST; it is not an empty document." >&2
    echo "Filing now would take the reader's open questions off the device with" >&2
    echo "nothing behind them. Render and deliver the follow-up first:" >&2
    echo "  $DIR/decisions-loop.sh render  --date $DATE" >&2
    echo "  $DIR/decisions-loop.sh deliver --date $DATE" >&2
    echo "file-processed is for the end of the loop, when render exits 13" >&2
    echo "with NOTHING REMAINS. Nothing was moved and the registry is" >&2
    echo "untouched." >&2
    return 14
  fi
  return 0
}

# file-processed: the end of the loop when nothing remains to ask. The
# document the reader answered leaves /Briefs for Previous Briefs (his 2026-09-13
# rule) and its registry entry goes with it, so the next replacement does not
# look for a document that is no longer there and stall on the harvest gate.
#
# The two steps are NOT equivalent in consequence and are not reported as one.
# The device move is the irreversible half; the registry update is local
# bookkeeping. If the move lands and the bookkeeping does not, that is a
# PARTIAL FAILURE: it exits non-zero, says exactly what moved and what the
# registry was expected to hold, and leaves the evidence for the repair. It
# used to swallow an unreadable or absent registry and print success over the
# top of it (the cross-reviewer review, mc-j5dr4), which is the shape that leaves a stale
# entry pointing at /Briefs for a document that is no longer there - and the
# next replacement stalls on the harvest gate with no idea why.
cmd_file_processed() {
  # The manifest here is the zero-card re-render, a different page from the one
  # The reader answered, so the consume gate is asked about the recorded consumed
  # identity instead. With no record (no render has run for this date) it falls
  # back to the manifest, which is then still the document on the device.
  local consumed_page source_manifest
  consumed_page="$(loop_state_get consumed_page_id)"
  consume_gate "$consumed_page" || return $?
  remaining_gate || return $?
  source_manifest="$(consumed_manifest_path "$consumed_page")" || {
    echo "The answered document's saved manifest is missing. Nothing was filed." >&2
    return 11
  }
  answer_gate "$source_manifest" || return $?
  "$RMAPI" mkdir "$PREV_FOLDER" >/dev/null 2>&1 || true
  if ! "$RMAPI" mv "$DEVICE_PATH" "$PREV_FOLDER" >/dev/null; then
    echo "FILING FAILED: $DEVICE_PATH could not be moved to $PREV_FOLDER." >&2
    echo "Nothing was deleted. The registry entry is left alone." >&2
    return 1
  fi
  local moved_to="$PREV_FOLDER/$NAME" registry_error
  # The helper prints an empty string on success and one plain-English line
  # naming the problem otherwise. It exits 0 either way so that a crash in it
  # is distinguishable from a diagnosis by it.
  registry_error="$("$PYTHON" - "$REGISTRY" "$DEVICE_PATH" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
if not path.exists():
    print(f"the registry file {path} does not exist")
    raise SystemExit(0)
try:
    registry = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"the registry at {path} could not be parsed: {exc}")
    raise SystemExit(0)
pages = registry.get("pages") if isinstance(registry, dict) else None
if not isinstance(pages, dict):
    print(f"the registry at {path} has no 'pages' object to remove the entry from")
    raise SystemExit(0)
if key not in pages:
    print(f"the registry at {path} held no entry for {key}, so none was removed")
    raise SystemExit(0)
pages.pop(key)
try:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(registry, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
except Exception as exc:
    print(f"the entry for {key} could not be written out of {path}: {exc}")
    raise SystemExit(0)
PY
)" || registry_error="the registry step could not be run at all"
  if [ -n "$registry_error" ]; then
    echo "PARTIAL FAILURE: the document was filed but the registry was not updated." >&2
    echo "" >&2
    echo "What DID happen, and is not undone by this exit:" >&2
    echo "  moved on the device: $DEVICE_PATH -> $moved_to" >&2
    echo "" >&2
    echo "What did NOT happen:" >&2
    echo "  registry:  $REGISTRY" >&2
    echo "  expected:  the entry keyed $DEVICE_PATH removed from .pages" >&2
    echo "  reason:    $registry_error" >&2
    echo "" >&2
    echo "Repair: make the registry readable, then remove that key by hand." >&2
    echo "A stale entry pointing at $DEVICE_PATH makes the next replacement" >&2
    echo "wait on a document that is no longer in $BRIEFS." >&2
    return 15
  fi
  echo "Processed Decisions document filed to $PREV_FOLDER: $NAME"
  echo "Registry entry removed: $DEVICE_PATH"
  return 0
}

case "$COMMAND" in
  status) cmd_status ;;
  render) cmd_render ;;
  deliver) cmd_deliver ;;
  file-processed) cmd_file_processed ;;
esac
