#!/usr/bin/env bash
# Enforce the reader's one-Decisions-document rule (the reader, 2026-09-13 13:30 PDT, his
# words): "when a new cumulative Decisions document is pushed to his
# reMarkable, the previous day's document leaves the Briefs folder: if it was
# not touched (no pen annotations, no tag) delete it; if he answered questions
# or took any action on it, move it to the previous-briefs folder on the
# tablet. On any day there is exactly one Decisions document in Briefs."
#
# This runs AFTER a decisions-<DATE> document is already live in /Briefs
# (push-decisions.sh calls it right after a successful --push). Every OTHER
# decisions-* document still sitting in /Briefs is inspected and:
#
#   untouched (no ink on any page, no doc- or page-level tag) -> deleted via
#     `rmapi mv <doc> /trash`. This is NOT `rmapi rm`: per the
#     remarkable-rmapi-rm-vs-trash-2026-09-09 memory, `rmapi rm` is a hard
#     delete that never appears in the device's own trash, while `rmapi mv
#     <doc> /trash` is what the device's own delete does and stays
#     recoverable there until the reader empties it. This script always uses mv.
#
#   touched (any ink, or a "ready" / "Marked as Read" tag at document or page
#     level - the same two tags check-annotations.sh and rotate-briefs.sh
#     recognize) -> moved into "/Briefs/Previous Briefs", the folder
#     rotate-briefs.sh already files answered Decisions documents into.
#     Nothing new is created; if that folder is missing this script fails
#     loudly rather than inventing a new one.
#
# Today's own document (--date) is never inspected or moved by this script.
#
# Usage:
#   rotate-decisions.sh --date YYYY-MM-DD [--briefs /Briefs]
#                        [--prev-folder "/Briefs/Previous Briefs"]
#
# Sourcing this file (rather than executing it) loads decide() and fetch()
# without running main - test-rotate-decisions.sh uses this to drive the
# disposition logic directly against fixture inputs, and a second, full-flow
# pass against a fake rmapi in the same style as test-rotate-briefs.sh.

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

RMAPI="${RMAPI:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rmapi-retry.sh}"
TAG_FLAGS="${TAG_FLAGS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tag_flags.py}"
LOG="${ROTATE_DECISIONS_LOG:-/tmp/remarkable-annotation-watch.log}"
EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# decide <inkhash> <docready> <docseen> <pageready> <pageseen> -> "touched"/"untouched"
#
# inkhash is "none" (verified no strokes), "?" (stat or fetch failed - the
# document could not be read), or a sha256 of real page ink. "?" is treated
# as touched: never auto-delete a document we could not actually verify.
decide() {
  local ink="$1" dready="$2" dseen="$3" pready="$4" pseen="$5"
  if [ "$ink" != "none" ]; then
    echo touched
    return
  fi
  if [ "$dready" = y ] || [ "$dseen" = y ] || [ "$pready" = y ] || [ "$pseen" = y ]; then
    echo touched
    return
  fi
  echo untouched
}

# duplicate_ids <visible-name> -> the document ids carrying that name, one
# per line, oldest first. rmapi has no command that prints ids, so they come
# from rmapi's own tree cache. Read-only, best effort: a missing or unreadable
# cache prints nothing rather than failing the rotation.
duplicate_ids() {
  python3 - "$1" <<'PY' 2>/dev/null || true
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
    print(f"    {doc_id}  lastModified={modified}")
PY
}

# fetch <device-path> -> "inkhash|docready|docseen|pageready|pageseen"
# Same two-stage read as check-annotations.sh's fetch_doc: `rmapi stat` for
# document-level tags, then a real `rmapi get` to hash the page ink and read
# page-level tags out of the .content file. Read-only; never mutates anything.
fetch() {
  local path="$1" statline mod dready dseen dir zip h content pready pseen

  statline=$("$RMAPI" stat "$path" 2>/dev/null | python3 "$TAG_FLAGS" stat)
  mod=$(printf '%s' "$statline" | cut -f1)
  dready=$(printf '%s' "$statline" | cut -f2)
  dseen=$(printf '%s' "$statline" | cut -f3)
  if [ -z "$mod" ]; then
    echo "?|?|?|?|?"
    return
  fi
  dready="${dready:-n}"
  dseen="${dseen:-n}"

  dir=$(mktemp -d)
  if ! ( cd "$dir" && "$RMAPI" get "$path" >/dev/null 2>&1 ); then
    rm -rf "$dir"
    echo "?|$dready|$dseen|n|n"
    return
  fi
  zip=$(find "$dir" -maxdepth 1 -type f | head -1)
  if [ -z "$zip" ]; then
    rm -rf "$dir"
    echo "?|$dready|$dseen|n|n"
    return
  fi
  h=$(unzip -p "$zip" '*.rm' 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  [ "$h" = "$EMPTY_SHA" ] && h="none"
  content=$(unzip -p "$zip" '*.content' 2>/dev/null | python3 "$TAG_FLAGS" content)
  pready="${content%%|*}"
  pseen="${content#*|}"
  rm -rf "$dir"
  echo "${h:-?}|$dready|$dseen|${pready:-n}|${pseen:-n}"
}

main() {
  local DATE="" BRIEFS="${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}" PREV="${RMW_TABLET_PREVIOUS_FOLDER:-${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}/Previous Briefs}"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --date) DATE="$2"; shift 2 ;;
      --briefs) BRIEFS="$2"; shift 2 ;;
      --prev-folder) PREV="$2"; shift 2 ;;
      *) echo "rotate-decisions.sh: unknown option: $1" >&2; return 2 ;;
    esac
  done
  if [ -z "$DATE" ]; then
    echo "rotate-decisions.sh: --date is required" >&2
    return 2
  fi

  local rc=0
  local today_seen=0
  local name combo ink dready dseen pready pseen rest disp
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    case "$name" in
      decisions-*) : ;;
      *) continue ;;
    esac
    if [ "$name" = "decisions-$DATE" ]; then
      # Today's own name is normally skipped: it is the document just
      # delivered. But the name is not an identity. On 2026-09-13 a delete
      # reported success and did not take, so /Briefs held TWO documents
      # called decisions-2026-09-13: the stale one the reader was penning, and the
      # fresh one. This loop skipped both, and the panel of "exactly one
      # Decisions document" was wrong for an hour with nobody told.
      #
      # A second document under today's name is now a candidate, not a
      # skip. It is never removed here. rmapi rm takes a path, and that path
      # matches both documents, so removing by name would take the wrong one
      # or both. The disposition is reported with the ids and left to a human.
      today_seen=$((today_seen + 1))
      [ "$today_seen" -le 1 ] && continue
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) rotate-decisions: DUPLICATE under today's name: $name" >> "$LOG"
      combo=$(fetch "$BRIEFS/$name")
      ink="${combo%%|*}"; rest="${combo#*|}"
      dready="${rest%%|*}"; rest="${rest#*|}"
      dseen="${rest%%|*}"; rest="${rest#*|}"
      pready="${rest%%|*}"; pseen="${rest#*|}"
      disp=$(decide "$ink" "$dready" "$dseen" "$pready" "$pseen")
      echo "  DUPLICATE under today's name, left in place: $name ($disp)" >&2
      if [ "$disp" = touched ]; then
        echo "  It carries ink or a tag. That is the reader's answers; it is never deleted." >&2
      else
        echo "  No ink and no tag found, but it is still not removed here:" >&2
        echo "  rmapi rm takes a path and that path matches both documents." >&2
      fi
      echo "  Document ids under this name:" >&2
      duplicate_ids "$name" >&2
      echo "  Inspect with: cd \"\$(mktemp -d)\" && rmapi get --id <id>" >&2
      rc=1
      continue
    fi

    combo=$(fetch "$BRIEFS/$name")
    ink="${combo%%|*}"; rest="${combo#*|}"
    dready="${rest%%|*}"; rest="${rest#*|}"
    dseen="${rest%%|*}"; rest="${rest#*|}"
    pready="${rest%%|*}"; pseen="${rest#*|}"

    if [ "$ink" = "?" ] && [ "$dready" = "?" ]; then
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) rotate-decisions: could not read $name, leaving it in place" >> "$LOG"
      echo "  UNREADABLE, left in place: $name" >&2
      rc=1
      continue
    fi

    disp=$(decide "$ink" "$dready" "$dseen" "$pready" "$pseen")
    if [ "$disp" = touched ]; then
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) rotate-decisions: touched (ink=$ink ready=$dready/$pready seen=$dseen/$pseen), filing to Previous Briefs: $name" >> "$LOG"
      if "$RMAPI" mv "$BRIEFS/$name" "$PREV" >/dev/null 2>&1; then
        echo "  moved to Previous Briefs: $name"
      else
        echo "  MOVE FAILED: $name" >&2
        rc=1
      fi
    else
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) rotate-decisions: untouched, deleting to trash: $name" >> "$LOG"
      if "$RMAPI" mv "$BRIEFS/$name" /trash >/dev/null 2>&1; then
        echo "  deleted (trash): $name"
      else
        echo "  DELETE FAILED: $name" >&2
        rc=1
      fi
    fi
  done < <("$RMAPI" ls "$BRIEFS" 2>/dev/null | awk -F'\t' '/^\[f\]/{print $2}')

  return "$rc"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
