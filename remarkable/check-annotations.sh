#!/usr/bin/env bash
# mc-nu3e: two-stage annotation-change detector for the reMarkable /Briefs tree.
# mc-k1tr (2026-07-26, the reader-approved): ready-tag protocol. The reader sets a "ready" tag
# (document level preferred; page level also honored) when a doc's annotations are
# ready for processing. Only tagged docs FIRE (print). Untagged ink changes advance
# the baseline with a quiet log line - no alert, no processing. Removing and
# re-applying the tag, or adding ink while the tag is present, re-fires. After FULL
# processing the AUTOMATION records the doc's ink hash as processed
# (unset-ready-tag.sh; mc-1nr9d correction 2026-09-04 - the old clear-the-tag path
# needed a delete-and-reupload that dropped processed Decisions documents from the
# tablet, so it no longer touches the device). The ready tag stays exactly as the reader
# set it: unchanged metadata is carried forward with no fire, and a changed ink
# hash while ready remains present fires again ("new ink while tag present" below).
# Decisions documents additionally wait for the reader's own Mark-as-Read tag before
# rotate-briefs.sh files them into Previous Briefs at the next 06:00 rotation.
#
#   check-annotations.sh            print docs ready for processing (see above)
#   check-annotations.sh --baseline refresh state silently (after push/rotation churn)
#
# Stage 1 (cheap, metadata only): rmapi stat ModifiedClient scan flags candidates and
# reads document-level Tags. Tag changes bump ModifiedClient (live-verified
# 2026-07-26), so tag transitions are always seen here.
# Stage 2 (per candidate only): rmapi get the doc archive; hash its *.rm page payloads
# (the actual pen strokes) and read page-level tags from the *.content file.
# (Origin 2026-07-23: three archived briefs false-fired on 07-22 after a move into
# Previous Briefs; the ink hash kills that false-positive class.)
#
# mc-ylap (2026-08-15, the reader pen-dictated): a SECOND tag, "Mark-as-Read", is his READ
# RECEIPT - he sets it when he has opened and finished reading a document. The two tags
# are independent and may be combined. Critical difference from "ready": mark-as-read is
# HIS state, not ours. Automation never sets it and never clears it, and it never fires
# the processing path on its own. A doc carrying only mark-as-read produces a quiet log
# line and nothing else. (unset-ready-tag.sh does not touch the device at all - mc-1nr9d,
# 2026-09-04: it records the processed ink hash in ~/.remarkable-processed-docs.tsv and
# leaves every tag, ready and mark-as-read alike, exactly as the reader set it.)
#
# The matcher NORMALISES rather than hardcoding one spelling: lowercase, then strip every
# non-alphanumeric character, then compare to "markasread". The reader dictated the tag as
# "Mark-as-Read" but no live instance existed on any watched document when this was built
# (2026-08-16), so the exact case and separators he types could not be read off the device.
# Normalising accepts Mark-as-Read, mark_as_read, "Mark As Read" and markasread alike, so
# the first real use cannot miss on punctuation.
#
# State: ~/.remarkable-annotation-state.json - TSV:
#   path<TAB>ModifiedClient<TAB>inkhash<TAB>ready<TAB>seen.
#   inkhash "?"    unknown (legacy rows / failed download): next ModifiedClient change
#                  conservative-fires IF the doc carries the ready tag.
#   inkhash "none" document has no pen strokes.
#   ready y/n      whether a "ready" tag (any case, doc or page level) was present.
#                  Legacy 3-field rows read as ready=n.
#   seen  y/n      whether a "mark-as-read" tag (normalised, doc or page level) was
#                  present. Legacy 4-field rows MIGRATE SILENTLY: a row whose metadata
#                  has not changed adopts the tag state currently on the device and
#                  logs nothing. Migration must not emit a burst of receipts for tags
#                  the reader set before the feature existed - a receipt means "he just
#                  finished reading this", and backdating that would be a lie.
#                  (An earlier revision of this comment claimed the opposite of what
#                  the code did; the cross-reviewer caught the disagreement 2026-08-16. Silence
#                  is the intended behaviour and the tests pin it.)
# Semantics unchanged: state advances every run, so a printed change must be handled in
# the same turn (safe because annotation-watch.sh sends a durable bus message).
#
# Processed state: ~/.remarkable-processed-docs.tsv - TSV:
#   path<TAB>inkhash<TAB>processed_at_iso8601.
# unset-ready-tag.sh writes this after all marks on a ready document have been
# handled. Since the ready tag intentionally remains on the device, the watcher
# must suppress a would-be ready alert when the current ink hash exactly matches
# the processed hash; ModifiedClient-only churn from syncs or device-side saves
# is not new work. A different hash still fires while ready is present.

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
# Both tag parsers live in tag_flags.py so the tests can execute the exact code
# this script runs (mc-ylap). Overridable so a test can point at a mutated copy
# and prove the suite fails when the matcher is broken.
TAG_FLAGS="${TAG_FLAGS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tag_flags.py}"
STATE=~/.remarkable-annotation-state.json
PROCESSED_STATE="${PROCESSED_STATE:-$HOME/.remarkable-processed-docs.tsv}"
LOG=/tmp/remarkable-annotation-watch.log
MODE="${1:-check}"

TMP_NEW=$(mktemp)
WORK=$(mktemp -d)
trap 'rm -f "$TMP_NEW"; rm -rf "$WORK"' EXIT

# sha256 of the doc's concatenated .rm pen payloads. "none" = no ink, "?" = fetch failed.
EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# fetch_doc <path> -> "inkhash|pagetagflag|pagereadflag"
#   pagetagflag  y/n = page-level "ready" tag
#   pagereadflag y/n = page-level "mark-as-read" tag (normalised, mc-ylap)
fetch_doc() {
  local dir zip h p
  dir=$(mktemp -d "$WORK/doc.XXXXXX")
  ( cd "$dir" && "$RMAPI" get "$1" >/dev/null 2>&1 ) || { echo "?|n|n"; return; }
  zip=$(find "$dir" -maxdepth 1 -type f | head -1)
  [ -z "$zip" ] && { echo "?|n|n"; return; }
  h=$(unzip -p "$zip" '*.rm' 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  [ "$h" = "$EMPTY_SHA" ] && h="none"
  p=$(unzip -p "$zip" '*.content' 2>/dev/null | python3 "$TAG_FLAGS" content)
  echo "$h|${p:-n|n}"
}

# stage1_row <path>: reads a raw `rmapi stat` payload on stdin and emits this
# path's Stage-1 row.
#
# UNREADABLE is not UNTAGGED (mc-ylap R3, the cross-reviewer). If stat fails or returns a
# payload we cannot make sense of, this used to emit nothing at all - which
# erased the path from the new state table and DELETED its stored row when the
# state file was replaced, so a later healthy run saw a brand-new document and
# could fire a false transition. Emit an explicit marker instead and let the
# state machine carry the existing row forward untouched.
#
# This is a function rather than inline code so the tests can drive the real
# decision with fixture payloads instead of reimplementing it (which is how the
# original suite came to pass against a matcher that matched nothing).
stage1_row() {
  local path="$1" line
  line=$(python3 "$TAG_FLAGS" stat)
  if [ -n "$line" ]; then
    printf '%s\t%s\n' "$path" "$line"
  else
    printf '%s\t__unreadable__\tn\tn\n' "$path"
  fi
}

# Stage 1: enumerate watched trees with ModifiedClient stamps + doc-level ready tag.
# Watch scope DERIVES FROM WHAT WE PUSH (the reader design point 2026-07-23, after his
# /Reviews annotations sat undetected under a hardcoded Briefs-only scan): push tools
# append their destination tree to ~/.remarkable-watched-trees; this reads it.
# Interim until rmfakecloud server-side hooks make scope moot.
TREES_FILE=~/.remarkable-watched-trees
# First run: watch the top folder of tablet.briefs_folder, plus /Reviews (where
# push-brief.sh --dest /Reviews puts documents sent out for margin review).
[ -f "$TREES_FILE" ] || printf '%s\n/Reviews\n' "/$(printf '%s' "${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}" | sed 's|^/||;s|/.*||')" > "$TREES_FILE"

# mc-rsh8 (2026-08-26): this scan was 3 find calls plus one stat per document,
# every 120 seconds, forever. At 28 documents that is 31 rmapi processes a
# cycle and about 22,300 cloud calls a day, which is 99.6 percent of all
# reMarkable traffic we generate and the reason the 429 storms happen. Two
# reductions, independent of each other.
#
# ARCHIVE THROTTLING (on by default). 17 of those 28 documents live in
# /Briefs/Previous Briefs and 2 in /Reviews/Previous - Filed. They are there
# because they have already been rotated out. The reader annotates today's brief, not
# last week's, so polling them every two minutes buys nothing. They are scanned
# once an hour instead. Cost: ink added to an ALREADY-ARCHIVED document is
# noticed up to an hour late instead of within two minutes. Nothing else
# changes, because a doc still in a live folder is still scanned every cycle.
# --baseline always scans everything: it runs right after rotation, which is
# exactly when the archive has changed.
#
# Skipped archive documents are re-emitted as __unreadable__ rather than
# omitted. That marker already means "could not read this document this run,
# preserve its row and change nothing" and is exactly right here. Omitting them
# instead would delete their stored rows and let them fire as brand-new
# documents on the next full scan (mc-ylap R3, the same bug class).
ARCHIVE_RE="${RM_ARCHIVE_PATTERN:-/Previous[^/]*/}"
ARCHIVE_INTERVAL="${RM_ARCHIVE_INTERVAL:-3600}"
# Deliberately derived from STATE rather than from $HOME: every test harness
# overrides STATE, and a stamp that stayed in $HOME would let a test run
# suppress the LIVE watcher's hourly archive scan for an hour.
ARCHIVE_STAMP="${RM_ARCHIVE_STAMP:-${STATE}.archive-stamp}"

SCAN_ARCHIVE=1
if [ "$MODE" != "--baseline" ]; then
  _now=$(date +%s)
  _last=$(cat "$ARCHIVE_STAMP" 2>/dev/null)
  case "$_last" in ''|*[!0-9]*) _last=0 ;; esac
  [ $((_now - _last)) -lt "$ARCHIVE_INTERVAL" ] && SCAN_ARCHIVE=0
fi

is_archived() { printf '%s\n' "$1" | grep -qE "$ARCHIVE_RE"; }

processed_hash_for() {
  local path="$1" slash_path no_slash_path
  [ -f "$PROCESSED_STATE" ] || return 0
  case "$path" in
    /*) slash_path="$path"; no_slash_path="${path#/}" ;;
    *) slash_path="/$path"; no_slash_path="$path" ;;
  esac
  awk -F'\t' -v a="$path" -v b="$slash_path" -v c="$no_slash_path" '
    $1 == a || $1 == b || $1 == c { found = $2 }
    END { if (found != "") print found }
  ' "$PROCESSED_STATE" 2>/dev/null
}

already_processed_hash() {
  local path="$1" hash="$2" processed
  [ -n "$hash" ] && [ "$hash" != "?" ] && [ "$hash" != "none" ] || return 1
  processed=$(processed_hash_for "$path")
  [ -n "$processed" ] && [ "$processed" = "$hash" ]
}

ready_fire() {
  local path="$1" hash="$2"
  if already_processed_hash "$path" "$hash"; then
    echo "$(date +%Y-%m-%dT%H:%M:%S%z) quiet: already processed ink hash, ready tag still present: $path" >> "$LOG"
    return
  fi
  echo "$path"
}

# Rows for archive documents we are deliberately not scanning this cycle.
carry_archived() {
  [ "$SCAN_ARCHIVE" = 0 ] || return 0
  [ -f "$STATE" ] || return 0
  cut -f1 "$STATE" | while read -r path; do
    [ -n "$path" ] || continue
    is_archived "$path" && printf '%s\t__unreadable__\tn\tn\n' "$path"
  done
}

# Scanner A: the original. One find per tree, then one stat per document.
# Cost: (trees) + (documents) rmapi processes.
stage1_find_stat() {
  sort -u "$TREES_FILE" | while read -r TREE; do
    [ -n "$TREE" ] && "$RMAPI" find "$TREE" 2>/dev/null
  done | grep '^\[f\]' | sed 's/^\[f\][[:space:]]*//' | while read -r path; do
    if [ "$SCAN_ARCHIVE" = 0 ] && is_archived "$path"; then
      continue
    fi
    "$RMAPI" stat "$path" 2>/dev/null | stage1_row "$path"
  done
}

# Scanner B (RM_STAGE1_JSON=1): one `rmapi -json ls` per DIRECTORY. That output
# carries modifiedClient and tags for every child, which is the whole of what
# stage 1 needs, so the per-document stat calls disappear entirely.
# Cost: (directories) rmapi processes, which is ~3 instead of ~31.
#
# Verified from rmapi v0.0.35 source (shell/output.go NodeJSON, shell/ls.go) and
# from the struct tags compiled into the installed binary. NOT yet verified
# against the live device, which is why this defaults to OFF. Flipping it on
# needs exactly one real `rmapi -json ls /Briefs` to confirm the payload shape.
#
# `-json find` was rejected for this: it walks recursively but flattens every
# entry to a bare name with no path, so paths would have to be rebuilt from
# parent UUIDs. Listing per directory keeps the path known by construction.
stage1_dirs() {
  sort -u "$TREES_FILE" | while read -r t; do [ -n "$t" ] && printf '%s\n' "${t%/}"; done
  # Subdirectories are discovered by a find sweep on archive cycles and read
  # back from the state file in between, so a folder the reader creates is picked up
  # within the hour rather than never.
  if [ "$SCAN_ARCHIVE" = 1 ]; then
    sort -u "$TREES_FILE" | while read -r TREE; do
      [ -n "$TREE" ] && "$RMAPI" find "$TREE" 2>/dev/null
    done | grep '^\[f\]' | sed 's/^\[f\][[:space:]]*//;s#/[^/]*$##'
  elif [ -f "$STATE" ]; then
    cut -f1 "$STATE" | sed 's#/[^/]*$##' | while read -r d; do
      [ -n "$d" ] && ! is_archived "$d/x" && printf '%s\n' "$d"
    done
  fi
}

stage1_json() {
  stage1_dirs | sort -u | while read -r dir; do
    [ -n "$dir" ] || continue
    "$RMAPI" -json ls "$dir" 2>/dev/null | python3 "$TAG_FLAGS" lsjson "$dir"
  done
}

# One seam, so a test harness substitutes exactly one function (the same way
# it substitutes fetch_doc) instead of splicing a region of this file by
# matching source text that any edit can move.
stage1_scan() {
  if [ "${RM_STAGE1_JSON:-0}" = "1" ]; then
    stage1_json
  else
    stage1_find_stat
  fi
  carry_archived
}

stage1_scan > "$TMP_NEW"

# A completely empty scan means the listing failed, not that the device was
# wiped. Treating it as real would delete every stored row. Keep the old state
# and try again next cycle.
if [ ! -s "$TMP_NEW" ] && [ -s "$STATE" ]; then
  echo "$(date +%Y-%m-%dT%H:%M:%S%z) stage 1 returned nothing; keeping previous state" >> "$LOG"
  exit 0
fi

[ "$SCAN_ARCHIVE" = 1 ] && date +%s > "$ARCHIVE_STAMP" 2>/dev/null

FIRST_RUN=0
[ -f "$STATE" ] || FIRST_RUN=1

NEW_STATE=$(mktemp "$WORK/state.XXXXXX")
while IFS=$'\t' read -r path mod doctag docseen; do
  # Exact first-field match. A substring grep matched "Briefs/decisions-X<TAB>" against
  # the archived "Briefs/Previous Briefs/decisions-X" row too, and after each hourly
  # archive scan (which lists Previous Briefs first) the live document read the archived
  # row as its own history: tag n->y every hour, a false fire every hour (2026-09-19).
  oldline=$(awk -F'\t' -v p="$path" '$1==p {print; exit}' "$STATE" 2>/dev/null)
  oldmod=$(printf '%s' "$oldline" | cut -f2)
  oldhash=$(printf '%s' "$oldline" | cut -f3)
  oldready=$(printf '%s' "$oldline" | cut -f4)
  oldseen=$(printf '%s' "$oldline" | cut -f5)

  if [ "$mod" = "__unreadable__" ]; then
    # Could not read this document's metadata this run. Preserve its row exactly
    # and change nothing: no fire, no quiet log, no baseline advance. If there is
    # no existing row we write none, because we have never successfully observed
    # this document and inventing a baseline from a failed read would be worse
    # than waiting for a run that works.
    [ -n "$oldline" ] && printf '%s\n' "$oldline" >> "$NEW_STATE"
    continue
  fi

  if [ "$mod" = "${oldmod:-}" ]; then
    # Metadata unchanged: carry known state forward (legacy rows -> "?"/doc-tag).
    printf '%s\t%s\t%s\t%s\t%s\n' "$path" "$mod" "${oldhash:-?}" "${oldready:-$doctag}" "${oldseen:-$docseen}" >> "$NEW_STATE"
    continue
  fi

  # Stage 2: metadata changed - check ink payload and page-level tags.
  if [ "$FIRST_RUN" = 1 ]; then
    hash="?"; ready="$doctag"; seen="$docseen"   # first ever run: baseline silently
  else
    combo=$(fetch_doc "$path")
    hash="${combo%%|*}"
    rest="${combo#*|}"
    ptag="${rest%%|*}"
    pseen="${rest#*|}"
    ready=n; { [ "$doctag" = "y" ] || [ "$ptag" = "y" ]; } && ready=y
    seen=n;  { [ "$docseen" = "y" ] || [ "$pseen" = "y" ]; } && seen=y
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$path" "$mod" "$hash" "$ready" "$seen" >> "$NEW_STATE"

  [ "$MODE" = "--baseline" ] && continue
  [ "$FIRST_RUN" = 1 ] && continue

  # mc-ylap read receipt. Logged on the n->y transition only, so a doc that keeps the
  # tag does not re-log on every ink change. Never fires the processing path, never
  # suppresses it either: a doc carrying BOTH tags still goes through the ready branch
  # below exactly as before.
  if [ "$seen" = "y" ] && [ "${oldseen:-n}" != "y" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S%z) read-receipt: the reader marked as read: $path" >> "$LOG"
  fi

  # mc-110b7 (the reader 2026-09-07): a path under Previous Briefs never fires, no
  # matter what changed on it (tag applied, tag removed, new ink). The row
  # above already carries the freshly scanned hash and tag forward, so
  # nothing is lost - it just cannot trigger processing while archived. His
  # signal to have it seen is moving the document out of the archive folder,
  # which is a path change and therefore a brand-new state-machine row
  # handled by the ordinary ready-tag rules below.
  if is_archived "$path"; then
    if [ -n "$oldline" ] && [ "$hash" != "${oldhash:-?}" ] && [ "$hash" != "?" ]; then
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) quiet: archived, ignored by rule mc-110b7: $path" >> "$LOG"
    fi
    continue
  fi

  if [ "$ready" = "y" ]; then
    if [ -z "$oldline" ]; then
      # New tagged doc: surface unless verifiably ink-free.
      [ "$hash" != "none" ] && ready_fire "$path" "$hash"
    elif [ "${oldready:-n}" != "y" ]; then
      # Tag transition (applied or re-applied): surface unless verifiably ink-free.
      [ "$hash" != "none" ] && ready_fire "$path" "$hash"
    elif [ "${oldhash:-?}" = "?" ] || [ "$hash" = "?" ]; then
      ready_fire "$path" "$hash"   # unknown either side while tagged: conservative-fire
    elif [ "$hash" != "$oldhash" ]; then
      ready_fire "$path" "$hash"   # new ink while tag present
    fi
  else
    # Untagged: never fire; quiet-log real ink changes so nothing is invisible.
    if [ -n "$oldline" ] && [ "$hash" != "${oldhash:-?}" ] && [ "$hash" != "?" ]; then
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) quiet: ink changed, no ready tag: $path" >> "$LOG"
    elif [ -z "$oldline" ] && [ "$hash" != "none" ]; then
      echo "$(date +%Y-%m-%dT%H:%M:%S%z) quiet: new doc with ink, no ready tag: $path" >> "$LOG"
    fi
  fi
done < "$TMP_NEW"

cp "$NEW_STATE" "$STATE"
exit 0
