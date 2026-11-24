#!/usr/bin/env bash
# mc-nu3e: rotate morning briefings on the reMarkable.
#
#   rotate-briefs.sh [--keep-days N]
#
# Instead of deleting yesterday's briefing, move it into "/Briefs/Previous Briefs"
# so the device keeps a short browsable history. Retention: docs in Previous Briefs
# older than N days (default 7, parsed from the morning-briefing-YYYY-MM-DD name)
# get their annotations pulled + archived to thoughts/remarkable-archive/, then are
# removed from the device. The repo archive is the permanent record; the device is
# a reading desk, not a filing cabinet.
#
# Annotation-pull failures never block the rotation: moving a doc loses nothing,
# so the pull is a harvest, not a gate. Run AFTER pushing today's brief. Finish by
# re-baselining the annotation-watch state so the rotation itself does not read as
# fresh annotations (check-annotations.sh --baseline). Decisions documents are
# never pruned from Previous Briefs (mc-ttiyq); only the daily families age out.
#
# Decisions documents (mc-1nr9d, the reader 2026-09-04, his words): "When I use the
# marked as read tag, what I want is for that document to be moved into the
# previous briefs folder, and it's okay for these moves to happen the next
# morning at 6 AM." So a decisions-* document is filed into Previous Briefs
# ONLY once the reader has set his "Mark-as-Read" tag on it - never on age or date
# alone, and never by unsetting or setting that tag ourselves; it is his read
# receipt. A decisions document without the tag stays exactly where it is, no
# matter how old, and this loop logs one line per document either way (moved,
# or held for the missing tag) so a quiet run is never mistaken for a working
# one. Since mc-1gx9o (2026-09-09) the same receipt rule covers EVERY non-daily
# document in /Briefs (assessments, briefs, designs pushed for the reader), not only
# decisions-*. The daily families (morning-briefing, morning-dashboard, today)
# keep their unconditional today/not-today rule. The tag the reader really sets reads
# "Marked as Read" on the device; the dictated "Mark-as-Read" is accepted too.

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
ARCHIVE_DIR=$RMW_ARCHIVE
KEEP_DAYS="${RMW_TABLET_KEEP_DAYS:-7}"
[ "${1:-}" = "--keep-days" ] && KEEP_DAYS="${2:-7}"
TODAY=$(date +%F)
BRIEFS="${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}"
PREV_FOLDER="${RMW_TABLET_PREVIOUS_FOLDER:-$BRIEFS/Previous Briefs}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$ARCHIVE_DIR"
"$RMAPI" mkdir "$PREV_FOLDER" >/dev/null 2>&1 || true  # idempotent

harvest() { # harvest <device-path> <name> : pull annotated PDF into the repo archive
  local path="$1" name="$2"
  (cd "$WORK" && "$RMAPI" geta "$path" >/dev/null 2>&1) || { echo "  harvest FAILED (non-fatal): $path"; return 1; }
  local pdf
  # shellcheck disable=SC2012  # rmapi creates these names in our private temp directory
  pdf=$(ls -t "$WORK"/*-annotations.pdf 2>/dev/null | head -1)
  if [ -n "$pdf" ]; then
    mv "$pdf" "$ARCHIVE_DIR/${name}-annotations.pdf"
    echo "  annotations archived: ${name}-annotations.pdf"
  fi
  rm -f "$WORK"/*.zip 2>/dev/null
  return 0
}

# mark_as_read_present <device-path> : "y"/"n" via a real rmapi stat, parsed by
# the same tag_flags.py module check-annotations.sh uses, so this reads the reader's
# tag with the one production matcher rather than a second reimplementation.
# Never sets or clears the tag; read-only.
mark_as_read_present() {
  local line seen
  line=$("$RMAPI" stat "$1" 2>/dev/null | python3 "$TAG_FLAGS" stat)
  seen=$(printf '%s' "$line" | cut -f3)
  [ "$seen" = "y" ]
}

# 1. Move any root-level briefing/dashboard/today that is not today's into
#    Previous Briefs, unconditionally (unchanged). Decisions documents are
#    gated on Mark-as-Read (mc-1nr9d) and never on the today/not-today check
#    alone: an old decisions document without the tag stays put. EVERY OTHER
#    document in /Briefs (briefs, assessments, designs pushed for the reader) is
#    gated on Mark-as-Read the same way and never on age (mc-1gx9o: four
#    briefs the reader tagged on Sept 3-4 sat here for five days because this loop
#    only looked at the daily names).
"$RMAPI" ls "$BRIEFS" 2>/dev/null | awk -F'\t' '/^\[f\]/{print $2}' | while read -r name; do
  [ "$name" = "morning-briefing-$TODAY" ] && continue
  [ "$name" = "morning-dashboard-$TODAY" ] && continue
  [ "$name" = "decisions-$TODAY" ] && continue
  [ "$name" = "today-$TODAY" ] && continue
  case "$name" in
    morning-briefing-*|morning-dashboard-*|today-*)
      echo "rotating: $name"
      ;;
    *)
      if ! mark_as_read_present "$BRIEFS/$name"; then
        echo "held (no Mark-as-Read yet): $name"
        continue
      fi
      echo "rotating (Mark-as-Read confirmed): $name"
      ;;
  esac
  harvest "$BRIEFS/$name" "$name" || true
  if "$RMAPI" mv "$BRIEFS/$name" "$PREV_FOLDER" >/dev/null 2>&1; then
    echo "  moved to Previous Briefs: $name"
  else
    echo "  MOVE FAILED: $name still in $BRIEFS (visible, harmless; retried next rotation)"
  fi
done

# 2. Retention: prune the daily families in Previous Briefs beyond KEEP_DAYS.
#    Decisions documents are NEVER pruned from the device (mc-ttiyq, the reader
#    2026-09-09: after he has answered one it is filed, not removed; he wants to
#    go back and look at them again). Only morning-briefing, morning-dashboard
#    and today pages age out; every decisions-* stays in Previous Briefs.
CUTOFF=$(date -v-"${KEEP_DAYS}"d +%F 2>/dev/null || date -d "-${KEEP_DAYS} days" +%F)
"$RMAPI" ls "$PREV_FOLDER" 2>/dev/null | awk -F'\t' '/^\[f\]/{print $2}' | grep -E '^(morning-(briefing|dashboard)|today)-' | while read -r name; do
  docdate=$(printf '%s' "$name" | sed -E 's/^(morning-(briefing|dashboard)|today)-//')
  [[ "$docdate" < "$CUTOFF" ]] || continue
  echo "pruning (older than ${KEEP_DAYS}d): $name"
  harvest "$PREV_FOLDER/$name" "$name" || true
  "$RMAPI" rm "$PREV_FOLDER/$name" >/dev/null 2>&1 && echo "  removed from device" || echo "  RM FAILED: $name"
done

# 3. Re-baseline annotation watch (rotation churn must not read as fresh pen marks).
"$(dirname "$0")/check-annotations.sh" --baseline >/dev/null 2>&1 || true

echo "--- $BRIEFS after rotation ---"
"$RMAPI" ls "$BRIEFS" 2>/dev/null
"$RMAPI" ls "$PREV_FOLDER" 2>/dev/null | head -10
