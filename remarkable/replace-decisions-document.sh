#!/usr/bin/env bash
# Safely deliver and register one Decisions document.
#
# A registered document is never replaced with rmapi put --content-only. The
# old raw document is archived first, then FILED to Previous Briefs with
# `rmapi mv`, then a fresh copy is pushed, verified by a fresh cloud listing,
# and registered. A durable journal makes the file-to-register window resumable
# without guessing. This script never calls `rmapi rm`: it is a hard delete
# with no undo, and nothing under /Briefs is worth that risk (mc-j5dr4).

set -euo pipefail

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
# Default to the 429-retry wrapper (mc-rsh8); bare rmapi mints a token per
# invocation and dies on the first rate limit, which stranded the corrected
# 2026-08-24 decisions push.
RMAPI="${RMAPI:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rmapi-retry.sh}"
PUSH="${DECISIONS_PUSH_HELPER:-$DIR/push-to-remarkable.sh}"
TODAY_PAGE_PY="${TODAY_PAGE_PY:-$DIR/today_page.py}"
PYTHON="${PYTHON:-python3}"

DATE=""
PDF=""
MANIFEST=""
OUT_DIR=""
REGISTRY=""
ARCHIVE_ROOT=""
DEVICE_PATH=""
EXTRACTOR=""
RECEIPT=""
PREV_FOLDER=""
TAG_FLAGS="${TAG_FLAGS:-$DIR/tag_flags.py}"
REVIEW_QUEUE="${REVIEW_QUEUE:-$RMW_PATHS_QUEUE}"
APPLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --date) DATE="$2"; shift 2 ;;
    --pdf) PDF="$2"; shift 2 ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --archive-root) ARCHIVE_ROOT="$2"; shift 2 ;;
    --device-path) DEVICE_PATH="$2"; shift 2 ;;
    --extractor-python) EXTRACTOR="$2"; shift 2 ;;
    # A second document for the same day (a supplement, mc-9jd4t) keeps its
    # own review receipt beside its own PDF. Unset means the receipt is found
    # where it always was, so the daily path is unchanged.
    --receipt) RECEIPT="$2"; shift 2 ;;
    # Where a processed document is FILED instead of deleted (the reader 2026-09-07,
    # 2026-09-13). Defaults to the Previous Briefs folder beside the device
    # path, which is the folder rotate-briefs.sh and rotate-decisions.sh use.
    --prev-folder) PREV_FOLDER="$2"; shift 2 ;;
    --review-queue) REVIEW_QUEUE="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -n "$DATE" ] || { echo "missing required option: DATE" >&2; exit 2; }
[ -n "$PDF" ] || { echo "missing required option: PDF" >&2; exit 2; }
[ -n "$MANIFEST" ] || { echo "missing required option: MANIFEST" >&2; exit 2; }
[ -n "$OUT_DIR" ] || { echo "missing required option: OUT_DIR" >&2; exit 2; }
[ -n "$REGISTRY" ] || { echo "missing required option: REGISTRY" >&2; exit 2; }
[ -n "$ARCHIVE_ROOT" ] || { echo "missing required option: ARCHIVE_ROOT" >&2; exit 2; }
[ -n "$DEVICE_PATH" ] || { echo "missing required option: DEVICE_PATH" >&2; exit 2; }
[ -n "$EXTRACTOR" ] || { echo "missing required option: EXTRACTOR" >&2; exit 2; }
[ -s "$PDF" ] || { echo "Decisions PDF is missing or empty" >&2; exit 1; }
[ -s "$MANIFEST" ] || { echo "Decisions manifest is missing or empty" >&2; exit 1; }
[ -x "$RMAPI" ] || { echo "rmapi is not executable" >&2; exit 1; }
[ -x "$PUSH" ] || { echo "Decisions push helper is not executable" >&2; exit 1; }

# Final-artifact review gate (mc-iacbw section 6). This is the last step
# before the document reaches the device, so the gate lives here as well as in
# push-decisions.sh: a caller that skips the wrapper must not be able to skip
# the review. Nothing is sent unless a receipt exists whose hashes match this
# exact PDF and manifest, written by someone other than the producer, with
# every card passing all three of the reader's tests. There is deliberately no way
# to switch this off: a bypass is the hole the gate exists to close.
if ! "$PYTHON" "$DIR/review-receipt.py" --archive-root "$ARCHIVE_ROOT" check \
    --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" \
    ${RECEIPT:+--receipt "$RECEIPT"} \
    --producer "${REMARKABLE_DOC_PRODUCER:-$RMW_WRITER_IDENTITY}"; then
  echo "REVIEW GATE: refusing to replace decisions-$DATE on the device." >&2
  exit 4
fi

DEST="$(dirname "$DEVICE_PATH")"
NAME="$(basename "$DEVICE_PATH")"
PREV_FOLDER="${PREV_FOLDER:-$DEST/Previous Briefs}"
JOURNAL_DIR="$ARCHIVE_ROOT/replacement-journals"
JOURNAL="$JOURNAL_DIR/decisions-$DATE.json"
RAW_ARCHIVE_DIR="$ARCHIVE_ROOT/replaced"
mkdir -p "$JOURNAL_DIR" "$RAW_ARCHIVE_DIR" "$OUT_DIR/processed" "$ARCHIVE_ROOT/receipts"

validate_registry() {
  [ ! -e "$REGISTRY" ] && return 0
  "$PYTHON" - "$REGISTRY" <<'PY'
import json
import pathlib
import sys

registry_document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(registry_document, dict) or not isinstance(registry_document.get("pages"), dict):
    raise SystemExit("REFUSING invalid Decisions registry")
PY
}

registry_has_page() {
  "$PYTHON" - "$REGISTRY" "$DEVICE_PATH" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    registry_document = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if sys.argv[2] in registry_document.get("pages", {}) else 1)
PY
}

registry_matches_manifest() {
  "$PYTHON" - "$REGISTRY" "$DEVICE_PATH" "$MANIFEST" <<'PY'
import json
import pathlib
import sys

registry = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
manifest_path = pathlib.Path(sys.argv[3]).resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
page = registry.get("pages", {}).get(sys.argv[2], {})
if not isinstance(manifest.get("page_id"), str) or not manifest["page_id"]:
    raise SystemExit(1)
matches = (
    page.get("manifest") == str(manifest_path)
    and page.get("page_id") == manifest["page_id"]
)
raise SystemExit(0 if matches else 1)
PY
}

# How many documents in $DEST carry today's exact name. rmapi ls prints one
# line per entry and never prints ids, so duplicates are only visible as a
# count. See remote_document_ids for the ids themselves.
#
# Counted, never short-circuited. The old remote_has_page ended in `grep -Fxq`,
# which exits the moment it matches; under `set -o pipefail` that can SIGPIPE
# the upstream `rmapi ls` and hand the whole pipeline a 141, so a document that
# IS there reads as absent. That is a coin flip on process scheduling, and the
# losing side of it skips the delete entirely (mc-9jd4t).
remote_page_count() {
  local listing
  listing="$("$RMAPI" ls "$DEST" 2>/dev/null || true)"
  printf '%s\n' "$listing" | awk -F'\t' '{print $2}' | grep -Fxc "$NAME" || true
}

remote_has_page() {
  [ "$(remote_page_count)" != "0" ]
}

# The document ids behind that name, newest last, read out of rmapi's own tree
# cache. rmapi has no command that prints ids, and `rmapi rm` takes a path, not
# an id, so these are for the operator to act on by hand with `rmapi get --id`.
remote_document_ids() {
  "$PYTHON" - "$NAME" <<'PY' 2>/dev/null || true
import json, pathlib, sys
name = sys.argv[1]
cache = pathlib.Path.home() / "Library" / "Caches" / "rmapi" / "tree.cache"
if not cache.is_file():
    raise SystemExit(0)
try:
    docs = json.loads(cache.read_text()).get("Docs", [])
except Exception:
    raise SystemExit(0)
rows = []
for doc in docs:
    meta = doc.get("Metadata") or {}
    if meta.get("visibleName") != name or meta.get("parent") == "trash":
        continue
    rows.append((meta.get("lastModified", ""), doc.get("DocumentID", "")))
for modified, doc_id in sorted(rows):
    print(f"  {doc_id}  lastModified={modified}")
PY
}

EMPTY_INK_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# archive_touch_state <raw-archive> -> prints exactly one word on stdout:
#
#   touched     the document carries the reader's marks (ink, or either tag)
#   untouched   the document is PROVABLY blank and untagged
#   unreadable  we could not establish either, for any reason
#
# The reader 2026-09-13, his words: a Decisions document he answered or acted on is
# MOVED to the previous-briefs folder, and only an untouched one is deleted.
# Both cases are now a MOVE (see retire_old_document), so this state is no
# longer a delete decision. It is still computed and still reported, because
# "unreadable" has to stop the run: a document we could not read is a document
# whose marks we cannot account for, and acting on it blind is how his ink gets
# lost. The same two signals rotate-decisions.sh uses decide touched here: any
# page ink, or either tag at document or page level.
#
# FAIL-CLOSED, and the failure is VISIBLE (the cross-reviewer review, mc-j5dr4). The
# earlier version of this function leaned on tag_flags.py, which fails SOFT by
# design: a malformed .content prints the untagged answer "n|n" and a failed
# `rmapi stat` prints an empty line, both indistinguishable from a real
# untagged document. That soft contract is right for the watcher, which must
# never delete a stored row over a transient parse failure, and wrong here,
# where the same silence read as "blank" and licensed a delete. So every
# payload is proved to be a JSON object FIRST, on its own, and only a payload
# that parsed is handed to tag_flags for the flags themselves.
archive_touch_state() {
  local archive="${1:-}" ink content_raw content statraw statline pready pseen dready dseen

  # No archive at all is no evidence at all.
  if [ -z "$archive" ] || [ ! -s "$archive" ]; then
    echo unreadable
    return 0
  fi
  # An archive that is not a readable zip tells us nothing. Hashing its
  # contents would produce the empty-ink hash and read as "blank", which is
  # exactly the wrong answer, so readability is proved first.
  if ! unzip -l "$archive" >/dev/null 2>&1; then
    echo unreadable
    return 0
  fi
  # unzip -p exits 11 when the glob matches nothing, and this runs under
  # `set -e -o pipefail`: without the `|| true` a document with no .rm member
  # would kill the script instead of being judged here. Every substitution in
  # this function is non-fatal on its own and judged on its VALUE.
  ink="$(unzip -p "$archive" '*.rm' 2>/dev/null | shasum -a 256 | cut -d' ' -f1 || true)"
  if [ -z "$ink" ]; then
    echo unreadable
    return 0
  fi
  if [ "$ink" != "$EMPTY_INK_SHA" ]; then
    echo touched
    return 0
  fi

  # The .content payload. Absent or unparseable is UNREADABLE, never blank.
  content_raw="$(unzip -p "$archive" '*.content' 2>/dev/null || true)"
  if [ -z "$content_raw" ] || ! printf '%s' "$content_raw" | is_json_object; then
    echo unreadable
    return 0
  fi
  content="$(printf '%s' "$content_raw" | "$PYTHON" "$TAG_FLAGS" content 2>/dev/null || true)"
  if ! printf '%s' "$content" | grep -Eq '^[yn]\|[yn]$'; then
    echo unreadable
    return 0
  fi
  pready="${content%%|*}"
  pseen="${content#*|}"
  if [ "$pready" = y ] || [ "$pseen" = y ]; then
    echo touched
    return 0
  fi

  # The document-level tags, from the cloud. A stat that failed, or that
  # returned something other than a JSON object, is UNREADABLE, never blank.
  if ! statraw="$("$RMAPI" stat "$DEVICE_PATH" 2>/dev/null)"; then
    echo unreadable
    return 0
  fi
  if [ -z "$statraw" ] || ! printf '%s' "$statraw" | is_json_object; then
    echo unreadable
    return 0
  fi
  statline="$(printf '%s' "$statraw" | "$PYTHON" "$TAG_FLAGS" stat 2>/dev/null || true)"
  dready="$(printf '%s' "$statline" | cut -f2 || true)"
  dseen="$(printf '%s' "$statline" | cut -f3 || true)"
  # tag_flags prints an EMPTY line when the payload has no usable mtime. The
  # payload parsed, so that is a shape we do not understand rather than an
  # answer, and it is unreadable too.
  if [ -z "$dready" ] || [ -z "$dseen" ]; then
    echo unreadable
    return 0
  fi
  if [ "$dready" = y ] || [ "$dseen" = y ]; then
    echo touched
    return 0
  fi
  echo untouched
}

# is_json_object: stdin parses as a JSON object. Exits 0 only then. This is the
# proof that a soft-failing parser's "no tags" answer is a real answer.
is_json_object() {
  "$PYTHON" -c 'import json,sys
try:
    doc = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if isinstance(doc, dict) else 1)' >/dev/null 2>&1
}

# retire_old_document <raw-archive> : get the old document out of the Briefs
# folder without ever destroying the reader's marks.
#
# EVERY retired document is MOVED to Previous Briefs with `rmapi mv`. Nothing
# here ever calls `rmapi rm` (the cross-reviewer review, mc-j5dr4; bd memory
# remarkable-rmapi-rm-vs-trash-2026-09-09: rmapi rm is a HARD delete, not a
# trash, and there is no undo on the device or in the cloud). The reader's 2026-09-13
# rule that an untouched document may be removed from /Briefs is satisfied by
# the move: the document leaves /Briefs, and Previous Briefs is pruned on its
# own 7-day schedule by rotate-briefs.sh. So the worst case of a wrong
# touched/untouched call is a blank sheet sitting in Previous Briefs for a
# week, instead of a week of the reader's answers gone for good.
#
# An UNREADABLE document stops the run. We cannot say whether it carries his
# marks, nothing has been uploaded yet at this point, and leaving it exactly
# where it is costs a delayed Decisions document and nothing else.
retire_old_document() {
  local archive="$1" state
  state="$(archive_touch_state "$archive" || true)"
  # Anything other than the two words that license a move is unreadable. A
  # function that died mid-way prints nothing, and nothing must never read as
  # permission to act on the reader's document.
  case "$state" in
    touched|untouched) : ;;
    *) state=unreadable ;;
  esac
  if [ "$state" = unreadable ]; then
    echo "UNREADABLE DOCUMENT: $DEVICE_PATH could not be read well enough to say" >&2
    echo "whether it carries the reader's marks, so it was NOT moved, NOT deleted, and" >&2
    echo "nothing was pushed. An unreadable document is never treated as blank." >&2
    echo "" >&2
    echo "Causes seen: the fetched archive is not a readable zip, its .content is" >&2
    echo "missing or malformed, or \`rmapi stat\` failed or returned a shape we do" >&2
    echo "not understand." >&2
    echo "" >&2
    echo "The raw archive of what was fetched is at:" >&2
    echo "  ${archive:-<none fetched>}" >&2
    echo "" >&2
    echo "Check it for ink by hand before doing anything to the device:" >&2
    echo "  unzip -l \"${archive:-<none fetched>}\" | grep '\\.rm\$'" >&2
    return 11
  fi
  "$RMAPI" mkdir "$PREV_FOLDER" >/dev/null 2>&1 || true
  if ! "$RMAPI" mv "$DEVICE_PATH" "$PREV_FOLDER" >/dev/null; then
    echo "FILING FAILED: $DEVICE_PATH could not be moved to $PREV_FOLDER." >&2
    echo "Nothing was deleted and nothing was pushed. Its state was: $state" >&2
    echo "Its raw archive is at:" >&2
    echo "  $archive" >&2
    return 10
  fi
  echo "Processed Decisions document filed to $PREV_FOLDER: $NAME ($state)"
  return 0
}

write_journal() {
  local state="$1" archive_path="${2:-}"
  "$PYTHON" - "$JOURNAL" "$DATE" "$DEVICE_PATH" "$PDF" "$MANIFEST" "$archive_path" "$state" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
journal_record = {
    "tool": "rmw-decisions-replacement-v1",
    "date": sys.argv[2],
    "device_path": sys.argv[3],
    "pdf": str(pathlib.Path(sys.argv[4]).resolve()),
    "manifest": str(pathlib.Path(sys.argv[5]).resolve()),
    "archive_path": str(pathlib.Path(sys.argv[6]).resolve()) if sys.argv[6] else "",
    "state": sys.argv[7],
}
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(journal_record, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PY
}

load_journal() {
  "$PYTHON" - "$JOURNAL" "$DATE" "$DEVICE_PATH" "$PDF" "$MANIFEST" "$RAW_ARCHIVE_DIR" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    journal_record = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"REFUSING unreadable Decisions replacement journal: {exc}")
expected = {
    "tool": "rmw-decisions-replacement-v1",
    "date": sys.argv[2],
    "device_path": sys.argv[3],
    "pdf": str(pathlib.Path(sys.argv[4]).resolve()),
    "manifest": str(pathlib.Path(sys.argv[5]).resolve()),
}
for key, expected_value in expected.items():
    if journal_record.get(key) != expected_value:
        raise SystemExit(f"REFUSING Decisions replacement journal with wrong {key}")
if journal_record.get("state") not in {"archived", "old_deleted", "new_pushed"}:
    raise SystemExit("REFUSING Decisions replacement journal with unknown state")
archive = pathlib.Path(journal_record.get("archive_path", ""))
root = pathlib.Path(sys.argv[6]).resolve()
try:
    archive.resolve().relative_to(root)
except (ValueError, RuntimeError):
    raise SystemExit("REFUSING Decisions replacement journal with archive outside its root")
if not archive.is_file() or archive.suffix != ".rmdoc":
    raise SystemExit("REFUSING Decisions replacement journal without its raw archive")
print(journal_record["state"])
print(str(archive.resolve()))
PY
}

register_page() {
  args=("$PYTHON" "$TODAY_PAGE_PY" register
    --registry "$REGISTRY"
    --device-path "$DEVICE_PATH"
    --manifest "$MANIFEST"
    --out-dir "$OUT_DIR/processed"
    --receipts-dir "$ARCHIVE_ROOT/receipts"
    --state "$ARCHIVE_ROOT/action-state.json"
    --extractor-python "$EXTRACTOR"
    --review-queue "$REVIEW_QUEUE")
  [ "$APPLY" -eq 0 ] || args+=(--apply)
  "${args[@]}"
}

state=""
archive_path=""
validate_registry
if [ -e "$JOURNAL" ]; then
  if ! journal_data="$(load_journal)"; then
    exit 8
  fi
  state="$(printf '%s\n' "$journal_data" | sed -n '1p')"
  archive_path="$(printf '%s\n' "$journal_data" | sed -n '2p')"
elif registry_has_page; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  if ! (cd "$work" && "$RMAPI" get "$DEVICE_PATH" >/dev/null); then
    # HARVEST FIRST (mc-9jd4t). On 2026-09-13 this fetch failed because the reader
    # had deleted the registered document, and the generic abort said only
    # "could not archive". He had penned SEVEN answers on it before deleting
    # it. They were in the tablet's trash, recorded nowhere, and the rebuilt
    # document was about to re-ask four of them. A registered document is
    # never replaced or abandoned until its ink has been harvested, and the
    # operator is told exactly how to harvest it.
    harvest="$ARCHIVE_ROOT/$DATE/harvest-receipt.json"
    if "$PYTHON" "$DIR/check_harvest_receipt.py" "$harvest" "$REGISTRY" "$DEVICE_PATH"; then
      echo "Decisions replacement: the registered document is gone from the device," >&2
      echo "and its ink was harvested ($harvest); continuing." >&2
      write_journal old_deleted ""
      state="old_deleted"
    else
      echo "HARVEST FIRST: the registered Decisions document at $DEVICE_PATH" >&2
      echo "cannot be fetched, so its ink cannot be archived. It may carry pen" >&2
      echo "answers that exist nowhere else. Nothing has been replaced or deleted." >&2
      echo "" >&2
      echo "If the reader deleted it, the copy is usually still in the tablet trash:" >&2
      echo "  rmapi ls /trash | grep decisions-$DATE" >&2
      echo "  cd \"\$(mktemp -d)\" && rmapi get /trash/decisions-$DATE" >&2
      echo "" >&2
      echo "Extract and resolve its ink, then write $harvest" >&2
      echo "with tool rmw-decisions-harvest-v1, the page_id, and the" >&2
      echo "ink_hash from the resolution. Re-run this push after that." >&2
      exit 5
    fi
  else
  raw_count="$(find "$work" -maxdepth 1 -type f -name '*.rmdoc' | wc -l | tr -d ' ')"
  [ "$raw_count" = "1" ] || { echo "ABORT: expected exactly one raw Decisions archive" >&2; exit 5; }
  raw="$(find "$work" -maxdepth 1 -type f -name '*.rmdoc' -print -quit)"
  stamp="$(date +%Y%m%d-%H%M%S)"
  unique_archive="$(mktemp "$RAW_ARCHIVE_DIR/$NAME-$stamp.XXXXXX")"
  archive_path="$unique_archive.rmdoc"
  mv "$unique_archive" "$archive_path"
  cp "$raw" "$archive_path"
  [ -s "$archive_path" ] || { echo "ABORT: raw Decisions archive is empty" >&2; exit 5; }
  cmp -s "$raw" "$archive_path" || { echo "ABORT: raw Decisions archive did not copy byte-for-byte" >&2; exit 5; }
  write_journal archived "$archive_path"
  state="archived"
  fi
elif remote_has_page; then
  echo "ABORT: an unregistered Decisions document already exists; refusing an ambiguous replacement" >&2
  exit 6
fi

if [ "$state" = "archived" ]; then
  if remote_has_page; then
    # 10 = the move itself failed; 11 = the document could not be read well
    # enough to act on it at all. Both leave it exactly where it is.
    retire_old_document "$archive_path" || exit $?
  else
    echo "Decisions replacement: old cloud document is already absent; resuming after delete"
  fi
  # DELETE MUST ACTUALLY TAKE (mc-9jd4t, 2026-09-13 17:05). The rm above
  # exited 0 and the document survived: the tablet had written a new version
  # 28 seconds earlier and the delete lost that race in silence. The upload
  # then landed beside it, so /Briefs held two documents with the same name,
  # and the reader penned four pages of answers onto the stale one. Nothing here
  # verified the removal, only that the NEW document arrived, which is the
  # one check that cannot see this failure.
  #
  # At this point nothing has been uploaded yet, so the correct count is
  # zero. Anything else means the delete did not take, and the upload is
  # refused rather than allowed to create a duplicate.
  remaining="$(remote_page_count)"
  if [ "$remaining" != "0" ]; then
    echo "OLD DOCUMENT STILL THERE: $remaining document(s) named $NAME still in" >&2
    echo "$DEST after it was filed or deleted, and nothing has been uploaded." >&2
    echo "Refusing to push a" >&2
    echo "second document with the same name." >&2
    echo "" >&2
    echo "The usual cause is the tablet writing a new version of the document" >&2
    echo "while the delete was in flight, which makes the delete a no-op that" >&2
    echo "still reports success." >&2
    echo "" >&2
    echo "Document ids behind that name:" >&2
    remote_document_ids >&2
    echo "" >&2
    echo "Check each for ink before removing anything:" >&2
    echo "  cd \"\$(mktemp -d)\" && rmapi get --id <id>" >&2
    echo "  unzip -l *.rmdoc | grep '\\.rm\$'" >&2
    echo "" >&2
    echo "A document with ink is the reader's answers and is never deleted. The" >&2
    echo "raw archive of what was fetched is at:" >&2
    echo "  $archive_path" >&2
    exit 9
  fi
  write_journal old_deleted "$archive_path"
  state="old_deleted"
fi

if [ -z "$state" ] || [ "$state" = "old_deleted" ]; then
  "$PUSH" "$PDF" "$DEST"
  if ! remote_has_page; then
    echo "VERIFY FAILED: fresh Decisions document is not visible in the cloud listing" >&2
    exit 4
  fi
  # And exactly one, not merely at least one (mc-9jd4t). "The new one arrived"
  # was the only post-upload check and it passes just as happily with a stale
  # twin sitting beside it.
  landed="$(remote_page_count)"
  if [ "$landed" != "1" ]; then
    echo "DUPLICATE AFTER PUSH: $landed documents named $NAME in $DEST, expected 1." >&2
    echo "Document ids behind that name:" >&2
    remote_document_ids >&2
    echo "" >&2
    echo "Check each for ink with rmapi get --id before removing anything. A" >&2
    echo "document with ink is the reader's answers and is never deleted." >&2
    exit 9
  fi
  if [ -n "$state" ]; then
    write_journal new_pushed "$archive_path"
  fi
  state="new_pushed"
fi

if [ "$state" = "new_pushed" ]; then
  if ! remote_has_page; then
    echo "VERIFY FAILED: journal says pushed but the Decisions document is absent" >&2
    exit 4
  fi
  register_page
  if ! registry_matches_manifest; then
    echo "VERIFY FAILED: fresh Decisions registration does not match its manifest" >&2
    exit 7
  fi
  rm -f "$JOURNAL"
fi

echo "Decisions delivered and registered: $DEVICE_PATH"
