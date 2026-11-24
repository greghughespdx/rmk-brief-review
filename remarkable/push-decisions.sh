#!/usr/bin/env bash
# Generate and deliver the daily Decisions document (mc-gx1o v2.1; successor
# to push-today.sh, retired with the v1 one-page layout 2026-07-28).
#
# Usage:
#   push-decisions.sh [--queue FILE] [--verify-report FILE] [--actions FILE]
#                     [--date YYYY-MM-DD] [--push] [--apply] [--no-generate]
#
# --queue defaults to a fresh verify-queue.py run (nothing renders unverified).
# --apply lets registered ticks execute. Without it, the watcher resolves ink
# and writes evidence but execution remains a dry run.
# --no-generate pushes the already-rendered, already-reviewed document for the
# date instead of rendering a new one over it.

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

MC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE="$(date +%F)"
QUEUE=""
VREPORT=""
ACTIONS=""
PUSH=0
APPLY=0
GENERATE=1
DEST="${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}"
NAME=""
REGISTRY="${REMARKABLE_TODAY_REGISTRY:-$RMW_PATHS_REGISTRY}"
ARCHIVE_ROOT="${REMARKABLE_TODAY_ARCHIVE:-$RMW_DECISIONS_ARCHIVE}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --queue) QUEUE="$2"; shift 2 ;;
    --verify-report) VREPORT="$2"; shift 2 ;;
    --actions) ACTIONS="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --no-generate) GENERATE=0; shift ;;
    --apply) APPLY=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# --name delivers a SECOND document for the same day under its own name (a
# supplement, mc-9jd4t): its own files, its own review receipt, its own
# registry entry, so pen answers on it resolve on their own. Only the day's
# own document is generated here, so a named document must already have been
# rendered and reviewed: --no-generate is required with it.
DEFAULT_NAME="decisions-$DATE"
NAME="${NAME:-$DEFAULT_NAME}"
if [ "$NAME" = "$DEFAULT_NAME" ]; then
  OUT_DIR="$ARCHIVE_ROOT/$DATE"
  RECEIPT="$OUT_DIR/review-receipt.json"
else
  if [ "$GENERATE" -ne 0 ]; then
    echo "--name renders nothing: pass --no-generate with an already rendered," >&2
    echo "already reviewed document named $NAME." >&2
    exit 2
  fi
  OUT_DIR="$ARCHIVE_ROOT/$DATE/$NAME"
  RECEIPT="$OUT_DIR/review-receipt.json"
fi
# reportlab lives in the pinned venv; the system python has no render deps.
GEN_PYTHON="${TODAY_EXTRACTOR_PYTHON:-$RMW_RENDER_PYTHON}"
PDF="$OUT_DIR/$NAME.pdf"
MANIFEST="$OUT_DIR/$NAME.manifest.json"

if [ "$GENERATE" -eq 0 ]; then
  # Nothing is being rendered, so there is nothing to verify freshness for.
  :
elif [ -z "$QUEUE" ] || [ -z "$VREPORT" ]; then
  QUEUE="/tmp/verified-queue-$DATE.md"
  VREPORT="$ARCHIVE_ROOT/verify-$DATE.json"
  mkdir -p "$ARCHIVE_ROOT"
  python3 "$DIR/verify-queue.py" --out-queue "$QUEUE" --report "$VREPORT" --render-date "$DATE" ${PHONE_CARDS_URL:+--live-cards "$PHONE_CARDS_URL"}
fi


# --no-generate pushes the document that was actually reviewed, rather than
# rendering a new one over the top of it. The review receipt is bound to the
# PDF's hash (mc-iacbw section 6), so the reviewed bytes are the ones that may
# be sent; re-rendering between the review and the push is what the freeze in
# the 04:00-to-06:00 sequence exists to prevent.
if [ "$GENERATE" -eq 0 ]; then
  if [ ! -f "$MANIFEST" ]; then
    echo "--no-generate was given but there is no document for $DATE at $OUT_DIR." >&2
    exit 2
  fi
else
  generation_status=0
  "$GEN_PYTHON" "$DIR/today_page.py" generate \
    --queue "$QUEUE" --verify-report "$VREPORT" --out-dir "$OUT_DIR" --date "$DATE" \
    ${ACTIONS:+--actions "$ACTIONS"} || generation_status=$?
  case "$generation_status" in
    0|6) : ;; # 6 means a manifest was written without rendering an empty PDF.
    5) exit 6 ;; # Keep a genuine renderer crash distinct from nothing to ask.
    *) exit "$generation_status" ;;
  esac
fi

# Empty-document tripwire (mc-4yv2): on 2026-08-23 and -24 every active row was
# held by verification and a blank sheet shipped silently. If the manifest holds
# zero cards while held or unaccounted active rows remain after answered and
# settled exclusions, that is a defect in the rows or the verifier - fail loudly (the 06:00 wrapper
# alerts on nonzero exit, mc-sguf) instead of printing the reader an empty page.
if ! counts=$(python3 - "$MANIFEST" <<'PY'
import json, pathlib, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())
cards = manifest["cards"]
held = manifest.get("held", [])
unresolved = manifest.get("unresolved_active_titles", [])
if not all(isinstance(value, list) for value in (cards, held, unresolved)):
    raise ValueError("Card and row counts require lists")
print(len(cards), max(len(held), len(unresolved)))
PY
); then
  echo "Could not read the Decisions card and row counts; nothing was pushed." >&2
  exit 2
fi
read -r CARDS ROWS_SEEN <<< "$counts"
if [ "$CARDS" -eq 0 ] && [ "$ROWS_SEEN" -gt 0 ]; then
  echo "EMPTY-DOC TRIPWIRE: $ROWS_SEEN held or unresolved row(s) remain but 0 cards are ready." >&2
  echo "Held reasons are in $VREPORT. Refusing to ship a blank Decisions sheet." >&2
  exit 3
fi

# Nothing to ask (the reader 2026-09-16). Zero cards AND zero active rows is not a
# failure and not a blank-sheet defect: it means the queue is empty and he has
# no decisions waiting. Distinguish it so the morning line can say "no questions
# for you" instead of reporting a withheld document.
if [ "$CARDS" -eq 0 ] && [ "$ROWS_SEEN" -eq 0 ]; then
  echo "No active queue rows and no cards: nothing to ask today." >&2
  exit 5
fi

if [ ! -f "$PDF" ]; then
  echo "The nonempty Decisions document has no PDF at $PDF." >&2
  exit 2
fi

if [ "$PUSH" -eq 0 ]; then
  echo "Generated locally. Add --push to send and register it."
  exit 0
fi

# Final-artifact review gate (mc-iacbw section 6). The reader asked for a second
# read of every card, every morning, by something that did not write it. The
# rule was prose and nothing enforced it, so a card could pass review and ship
# in a form the review never saw. Nothing is pushed without a receipt whose
# hashes match this exact PDF and manifest and whose every card passed all
# three of his tests. The reasons are printed; the document stays unsent.
PRODUCER="${REMARKABLE_DOC_PRODUCER:-$RMW_WRITER_IDENTITY}"
if ! python3 "$DIR/review-receipt.py" --archive-root "$ARCHIVE_ROOT" check \
    --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
    --producer "$PRODUCER"; then
  echo "REVIEW GATE: refusing to push decisions-$DATE." >&2
  exit 4
fi

# Delivery-time phone-card gate (mc-0mtb0, the cross-reviewer's review of c19bb9a2). The
# 06:00 job delivers the 05:00 reviewed bytes with --no-generate, so the
# render-time gate in verify-queue.py cannot see a phone question card opened or
# answered since that render. This reads the exact manifest being delivered,
# never re-renders it, and withholds the delivery (exit 7) when a printed
# question has a phone card that is open, answered or discarded. The reviewed
# PDF and manifest are left exactly as they are; a fresh render and review are
# needed. A service that cannot be read does not withhold; the report says so.
# Optional: runs only when PHONE_CARDS_URL names a phone question service.
LIVE_CHECK="${PHONE_CARDS_LIVE_CHECK:-$DIR/live-card-delivery-check.py}"
LIVE_REPORT="$ARCHIVE_ROOT/live-cards-$NAME.json"
if [ -z "${PHONE_CARDS_URL:-}" ]; then
  : # no phone question service configured; nothing to cross-check
elif [ ! -f "$LIVE_CHECK" ]; then
  echo "LIVE-CARD GATE: $LIVE_CHECK is missing; refusing to push $NAME." >&2
  exit 7
fi
live_rc=0
if [ -n "${PHONE_CARDS_URL:-}" ]; then
  python3 "$LIVE_CHECK" --manifest "$MANIFEST" --live-cards "$PHONE_CARDS_URL" \
    --report "$LIVE_REPORT" || live_rc=$?
fi
if [ "$live_rc" -ne 0 ]; then
  echo "LIVE-CARD GATE: refusing to push $NAME (check exit $live_rc). Report: $LIVE_REPORT." >&2
  echo "The document is unchanged; it needs a fresh render and review." >&2
  exit 7
fi

EXTRACTOR_PYTHON="${TODAY_EXTRACTOR_PYTHON:-$RMW_RENDER_PYTHON}"
"$EXTRACTOR_PYTHON" -c \
  'import importlib.metadata; assert importlib.metadata.version("rmscene") == "0.8.0"'

DELIVER=("$DIR/replace-decisions-document.sh"
  --date "$DATE"
  --pdf "$PDF"
  --manifest "$MANIFEST"
  --out-dir "$OUT_DIR"
  --registry "$REGISTRY"
  --archive-root "$ARCHIVE_ROOT"
  --receipt "$RECEIPT"
  --device-path "$DEST/$NAME"
  --extractor-python "$EXTRACTOR_PYTHON"
  --review-queue "$RMW_PATHS_QUEUE")
[ "$APPLY" -eq 0 ] || DELIVER+=(--apply)
"${DELIVER[@]}"

# One-Decisions-document-in-Briefs rule (the reader, 2026-09-13 13:30 PDT): once
# today's document is confirmed live above, every OTHER decisions-* document
# still in $DEST either gets filed to "$DEST/Previous Briefs" (it has ink or a
# tag - he did something with it) or deleted to the tablet's own trash (no ink,
# no tag - it was never touched). Runs only after the --push above succeeds;
# never runs on a dry-run (`--push` omitted) or after a failed delivery, since
# set -e above would already have exited. rotate-decisions.sh never uses
# `rmapi rm`; it always trashes via `rmapi mv <doc> /trash`, which stays
# recoverable on the device until the reader empties it.
# Only the day's own document clears the folder. A supplement delivered with
# --name is an ADDITION to today's document, not a replacement for it, and
# rotate-decisions.sh judges by name alone - run from a supplement it would
# file or trash the very document the supplement completes (mc-9jd4t).
if [ "$NAME" = "$DEFAULT_NAME" ]; then
  "$DIR/rotate-decisions.sh" --date "$DATE" --briefs "$DEST"
else
  echo "Supplement delivered as $NAME; today's own document is left in place."
fi
