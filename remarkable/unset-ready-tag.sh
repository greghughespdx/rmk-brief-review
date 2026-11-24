#!/usr/bin/env bash
# unset-ready-tag.sh <device-path>
#
# Corrected contract (the reader, 2026-09-04, mc-1nr9d): automation never deletes or
# re-uploads a document the reader is reading, for any reason. The previous approach
# here (rmapi rm, then rmapi put of a rebuilt copy, because rmapi has no
# tag-edit command) is exactly what made processed Decisions documents vanish
# from the tablet: the cloud listing still showed the document at /Briefs, but
# the device did not. rmapi's own put --content-only path is documented
# elsewhere in this directory (replace-decisions-document.sh) as able to "fork
# the cloud entry and make the tablet drop the document" - the same failure
# class, a second way to reach it. Live-verified: `rmapi` (see `rmapi help`)
# exposes exactly ls, stat, find, get, geta, mget, mv, put, mput, mkdir, rm,
# nuke - no tag-edit command of any kind, and no metadata-only update. Neither
# form of "safe re-upload" is safe, so this script no longer touches the
# device.
#
# "Processed" is now tracked on OUR side, keyed by the same ink hash
# check-annotations.sh already computes into
# ~/.remarkable-annotation-state.json. This script records a row in
# ~/.remarkable-processed-docs.tsv: path, ink hash, and when it was processed.
# The ready tag stays exactly as the reader set it, on the device, in its folder,
# forever, until he changes it himself - his ink is authoritative. Re-firing on
# NEW ink still works with no tag change at all: check-annotations.sh already
# fires whenever the ink hash changes while "ready" is present (the "new ink
# while tag present" branch), independent of any tag transition. Nothing about
# that dedup logic depended on clearing the tag; it depended on the ink hash,
# which is untouched by leaving the tag alone.
#
# Usage: unset-ready-tag.sh </Folder/doc-name>
# Recorded state: PROCESSED_STATE (default ~/.remarkable-processed-docs.tsv),
# a TSV of path<TAB>inkhash<TAB>processed_at_iso8601, one row per path (later
# calls replace the row for the same path rather than accumulating history).

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
PROCESSED_STATE="${PROCESSED_STATE:-$HOME/.remarkable-processed-docs.tsv}"
LOG="${UNSET_READY_LOG:-/tmp/remarkable-annotation-watch.log}"
DEVPATH="${1:?usage: unset-ready-tag.sh </Folder/doc-name>}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# ink_hash <device-path> -> sha256 of the doc's concatenated .rm pen payloads,
# "none" if the doc has no strokes, "?" if the download failed. Same recipe as
# check-annotations.sh's fetch_doc, kept standalone so this script never has to
# source the watcher (and never inherits its rmapi-mutating behavior).
ink_hash() {
  local dir zip h
  dir=$(mktemp -d "$WORK/doc.XXXXXX")
  ( cd "$dir" && "$RMAPI" get "$1" >/dev/null 2>&1 ) || { echo "?"; return; }
  zip=$(find "$dir" -maxdepth 1 -type f | head -1)
  [ -z "$zip" ] && { echo "?"; return; }
  h=$(unzip -p "$zip" '*.rm' 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  [ "$h" = "$EMPTY_SHA" ] && h="none"
  echo "$h"
}

HASH=$(ink_hash "$DEVPATH")
if [ "$HASH" = "?" ]; then
  echo "FAILED: could not read $DEVPATH to record its processed ink hash (nothing on the device was touched)"
  exit 1
fi

NOW=$(date +%Y-%m-%dT%H:%M:%S%z)
: > "$WORK/new-state.tsv"
if [ -f "$PROCESSED_STATE" ]; then
  awk -F'\t' -v p="$DEVPATH" '$1!=p' "$PROCESSED_STATE" > "$WORK/new-state.tsv" 2>/dev/null || true
fi
printf '%s\t%s\t%s\n' "$DEVPATH" "$HASH" "$NOW" >> "$WORK/new-state.tsv"
mkdir -p "$(dirname "$PROCESSED_STATE")"
cp "$WORK/new-state.tsv" "$PROCESSED_STATE"

echo "processed: $DEVPATH (inkhash $HASH) recorded in $PROCESSED_STATE; document left in place, ready tag untouched"
echo "$NOW processed: $DEVPATH (inkhash $HASH)" >> "$LOG" 2>/dev/null || true
