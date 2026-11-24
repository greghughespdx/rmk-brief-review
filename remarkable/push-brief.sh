#!/usr/bin/env bash
# Push a markdown brief to the reMarkable as a clean single-column PDF.
#
#   push-brief.sh <input.md> [--title "Doc Title"] [--dest /Briefs] [--replace]
#                 [--paper tablet|letter] [--engine chrome|reportlab]
#                 [--pdf-only OUT.pdf]
#
# Chain (engine chrome, the default): pandoc (md -> standalone HTML with the
# stylesheet inlined) -> headless Chrome (--print-to-pdf; no LaTeX toolchain
# needed) -> rmapi put. --paper tablet uses brief.css (a page shaped like the
# tablet screen, e-ink type sizes); --paper letter uses print-letter.css (US
# Letter, for anything that will also be printed: a tablet-sized PDF printed on
# Letter comes out with huge type and cut-off tables). Letter PDFs read fine on
# the tablet too.
#
# Engine reportlab renders with templates/brief.py instead: no browser, no
# pandoc, US Letter with a wide annotation margin. It does not draw tables.
#
# --pdf-only renders to OUT.pdf and stops: no tablet call at all.
#
# --replace: if a doc with the same visible name exists at the destination,
#   its annotations are pulled (rmapi geta) and archived to
#   thoughts/remarkable-archive/replaced/ FIRST, then the old copy is removed
#   and the new one uploaded (geta -> rm -> put; put-first is unsafe because
#   two same-name docs make the rm path ambiguous). A failed annotation pull
#   ABORTS the replace. WITHOUT --replace an existing same-name doc makes
#   the script abort so annotations are never silently orphaned.
#
# Verify-at-the-real-surface: after upload the script lists the destination
# folder and fails unless the new doc is present.

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

RMAPI="${RMAPI:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rmapi-retry.sh}"
CHROME="${CHROME:-${RMW_RENDER_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}}"
PANDOC="${PANDOC:-${RMW_RENDER_PANDOC:-pandoc}}"
ENGINE="${RMW_RENDER_BRIEF_ENGINE:-chrome}"
HERE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSS="$HERE_DIR/brief.css"
PDF_ONLY=""

INPUT="${1:?usage: push-brief.sh <input.md> [--title t] [--dest /Briefs] [--replace]}"
shift || true

TITLE=""
DEST="${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}"
REPLACE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --paper)
      case "$2" in
        tablet) CSS="$HERE_DIR/brief.css" ;;
        letter) CSS="$HERE_DIR/print-letter.css" ;;
        *) echo "--paper takes tablet or letter" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --engine) ENGINE="$2"; shift 2 ;;
    --pdf-only) PDF_ONLY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -f "$INPUT" ] || { echo "no such file: $INPUT" >&2; exit 1; }
case "$ENGINE" in
  chrome) [ -x "$CHROME" ] || { echo "Chrome not found at $CHROME (set render.chrome, or use --engine reportlab)" >&2; exit 1; } ;;
  reportlab) : ;;
  *) echo "--engine takes chrome or reportlab" >&2; exit 2 ;;
esac
[ -n "$PDF_ONLY" ] || [ -x "$RMAPI" ] || { echo "rmapi not found at $RMAPI" >&2; exit 1; }

BASE="$(basename "$INPUT" .md)"
[ -n "$TITLE" ] || TITLE="$BASE"

# Decisions documents are never removed from the device by this tool (mc-ttiyq,
# The reader 2026-09-09: an answered decisions document is filed, not deleted). The
# --replace path below does geta -> rm -> put, which would delete one. Fail
# closed here, before any rendering or any device call; the reviewed path for
# refreshing a decisions document is push-decisions.sh (and its
# replace-decisions-document.sh gate), which archives, journals and registers.
case "$BASE" in
  decisions-*)
    if [ "$REPLACE" -eq 1 ]; then
      echo "REFUSED: '$BASE' is a decisions document; push-brief.sh --replace would delete it from the device." >&2
      echo "Use remarkable/push-decisions.sh (review gate, journal, registration) to refresh a decisions document." >&2
      exit 6
    fi
    ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

HTML="$WORK/$BASE.html"
PDF="$WORK/$BASE.pdf"

# Subtitle line under the H1: generation date, stamped by the rail not the author.
META="Prepared $(date '+%A, %B %-d %Y %H:%M %Z')"

if [ "$ENGINE" = reportlab ]; then
  "${RMW_RENDER_PYTHON:-python3}" "$HERE_DIR/templates/brief.py" "$INPUT" "$PDF" "$TITLE" >/dev/null
else
  "$PANDOC" "$INPUT" \
    --standalone \
    --metadata "title=$TITLE" \
    --metadata "subtitle=$META" \
    --css "$CSS" \
    --embed-resources \
    -f gfm+smart \
    -t html5 \
    -V "lang=en" \
    -o "$HTML"

  "$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --print-to-pdf="$PDF" "file://$HTML" 2>/dev/null
fi

[ -s "$PDF" ] || { echo "PDF generation produced nothing" >&2; exit 1; }

if [ -n "$PDF_ONLY" ]; then
  cp "$PDF" "$PDF_ONLY"
  echo "rendered: $PDF_ONLY (not pushed)"
  exit 0
fi

# Destination folder (idempotent)
"$RMAPI" mkdir "$DEST" >/dev/null 2>&1 || true

EXISTING=$("$RMAPI" ls "$DEST" 2>/dev/null | awk -F'\t' '{print $2}' | grep -Fx "$BASE" || true)
if [ -n "$EXISTING" ] && [ "$REPLACE" -eq 0 ]; then
  echo "ABORT: '$BASE' already exists in $DEST. Pull annotations first (rmapi geta)," >&2
  echo "archive them, then re-run with --replace." >&2
  exit 3
fi

if [ -n "$EXISTING" ] && [ "$REPLACE" -eq 1 ]; then
  # ALWAYS pull annotations before retiring the old copy (mc-olw6; the
  # reMarkable hygiene rule - never destroy a device copy whose ink is not
  # archived). Sequence is geta+archive -> rm -> put: put-then-rm is NOT
  # safe with rmapi because two same-name docs make the rm path ambiguous,
  # so a brief zero-copy window is accepted; the ink is already archived.
  # Two same-name copies make every path ambiguous: refuse, so no copy is
  # retired without its own archive (2026-11-19: a duplicate was hard-deleted
  # unarchived). Resolve duplicates by hand, archiving each, then re-run.
  DUP_COUNT="$("$RMAPI" ls "$DEST" 2>/dev/null | awk -v b="$BASE" '$1=="[f]" { $1=""; sub(/^[ \t]+/,""); if ($0==b) n++ } END { print n+0 }')"
  if [ "$DUP_COUNT" -gt 1 ]; then
    echo "REFUSED: $DUP_COUNT copies of '$BASE' in $DEST; archive and remove the extras one at a time first." >&2
    exit 6
  fi
  ARCHIVE_DIR="$RMW_ARCHIVE/replaced"
  mkdir -p "$ARCHIVE_DIR"
  STAMP="$(date +%Y%m%d-%H%M%S)"
  GETA_TMP="$(mktemp -d)"
  # Mandatory: raw .rmdoc archive (always retrievable, preserves any ink).
  if ! (cd "$GETA_TMP" && "$RMAPI" get "$DEST/$BASE" >/dev/null 2>&1) \
     || ! ls "$GETA_TMP"/*.rmdoc >/dev/null 2>&1; then
    echo "ABORT: could not archive '$DEST/$BASE' (rmapi get) before replace" >&2
    rm -rf "$GETA_TMP"
    exit 5
  fi
  cp "$GETA_TMP"/*.rmdoc "$ARCHIVE_DIR/${BASE}-${STAMP}.rmdoc"
  # Best-effort: rendered annotations PDF (fails harmlessly on ink-free docs).
  if (cd "$GETA_TMP" && "$RMAPI" geta "$DEST/$BASE" >/dev/null 2>&1); then
    GOT_PDF="$(ls "$GETA_TMP"/*.pdf 2>/dev/null | head -1 || true)"
    [ -n "$GOT_PDF" ] && cp "$GOT_PDF" "$ARCHIVE_DIR/${BASE}-${STAMP}.pdf"
  fi
  rm -rf "$GETA_TMP"
  # Retire to the device trash (recoverable until the reader empties it), never rm.
  "$RMAPI" mv "$DEST/$BASE" /trash >/dev/null
fi

"$RMAPI" put "$PDF" "$DEST" >/dev/null

# Verify at the real surface
"$RMAPI" ls "$DEST" | awk -F'\t' '{print $2}' | grep -Fx "$BASE" >/dev/null \
  || { echo "VERIFY FAILED: '$BASE' not visible in $DEST after upload" >&2; exit 4; }

# Auto-register the destination tree in the annotation watcher's scope (the reader design
# point 2026-07-23: watch coverage derives from what we push, never a hardcoded list).
TREES_FILE=~/.remarkable-watched-trees
TOP="/$(printf '%s' "$DEST" | sed 's|^/||;s|/.*||')"
grep -Fxq "$TOP" "$TREES_FILE" 2>/dev/null || echo "$TOP" >> "$TREES_FILE"

echo "pushed: $DEST/$BASE ($(du -h "$PDF" | cut -f1 | tr -d ' '))"
