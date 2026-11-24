#!/usr/bin/env bash
# push-to-remarkable.sh -- render (if needed) and push a file to the reMarkable
#
# Usage:
#   push-to-remarkable.sh <file> [destination_folder]
#   push-to-remarkable.sh <file> [destination_folder] --template <brief|review-doc>
#
# Arguments:
#   file              Markdown (.md) or PDF (.pdf) to push
#   destination_folder  reMarkable folder (default: /Briefs)
#   --template        brief | review-doc
#                     For .md files: which template to render with.
#                     Default: brief
#
# This is the no-browser brief path: the templates are pure reportlab
# (templates/brief.py, templates/review-doc.py), US Letter with a wide right
# margin for pen notes. push-brief.sh is the richer path (pandoc + headless
# Chrome, tablet-sized pages, the replace and harvest safeguards).
#
# Environment:
#   RMAPI            Override the rmapi command (default: rmapi-retry.sh beside this script)
#   PYTHON3          Override the python3 used to render (default: first one with reportlab)
#
# Auth: rmapi keeps its own device token (run `rmapi` once interactively to
#       pair). Never commit or copy that token file.
#
# Examples:
#   push-to-remarkable.sh notes/weekly-brief.md
#   push-to-remarkable.sh notes/weekly-brief.md /Handoffs
#   push-to-remarkable.sh docs/design-draft.md /Reviews --template review-doc

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"

# ---- Defaults ----
RMAPI="${RMAPI:-$SCRIPT_DIR/rmapi-retry.sh}"
DEFAULT_DEST="${RMW_TABLET_BRIEFS_FOLDER:-/Briefs}"
DEFAULT_TEMPLATE="brief"

# ---- Detect system python3 with reportlab ----
detect_python() {
    # Prefer /usr/bin/python3 (system 3.9) which has reportlab installed
    for py in ${RMW_RENDER_PYTHON:-} "$SCRIPT_DIR/../.venv/bin/python" python3 /usr/bin/python3; do
        if command -v "$py" &>/dev/null; then
            if "$py" -c "import reportlab" 2>/dev/null; then
                echo "$py"
                return 0
            fi
        fi
    done
    echo "ERROR: No python3 with reportlab found. Install: pip install -r requirements.txt" >&2
    return 1
}

PYTHON3="${PYTHON3:-$(detect_python)}"

# ---- Parse arguments ----
INPUT_FILE=""
DEST_FOLDER="$DEFAULT_DEST"
TEMPLATE="$DEFAULT_TEMPLATE"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --template|-t)
            TEMPLATE="$2"
            shift 2
            ;;
        --help|-h)
            head -30 "${BASH_SOURCE[0]}" | grep "^#" | sed 's/^# //'
            exit 0
            ;;
        *)
            if [[ -z "$INPUT_FILE" ]]; then
                INPUT_FILE="$1"
            else
                DEST_FOLDER="$1"
            fi
            shift
            ;;
    esac
done

if [[ -z "$INPUT_FILE" ]]; then
    echo "Usage: push-to-remarkable.sh <file.md|file.pdf> [/Folder] [--template brief|review-doc]" >&2
    exit 1
fi

if [[ ! -f "$INPUT_FILE" ]]; then
    echo "ERROR: File not found: $INPUT_FILE" >&2
    exit 1
fi

if [[ ! -x "$RMAPI" ]]; then
    echo "ERROR: rmapi not found or not executable at $RMAPI" >&2
    exit 1
fi

# ---- Determine if we need to render ----
EXT="${INPUT_FILE##*.}"
BASENAME="$(basename "$INPUT_FILE" ."$EXT")"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PDF_FILE=""

if [[ "$EXT" == "pdf" ]]; then
    PDF_FILE="$INPUT_FILE"
    echo "Input is already a PDF, skipping render."
elif [[ "$EXT" == "md" || "$EXT" == "markdown" ]]; then
    PDF_FILE="$TMP_DIR/${BASENAME}.pdf"
    echo "Rendering $INPUT_FILE -> $PDF_FILE (template: $TEMPLATE)"

    case "$TEMPLATE" in
        brief)
            TEMPLATE_SCRIPT="$TEMPLATES_DIR/brief.py"
            ;;
        review-doc|review)
            TEMPLATE_SCRIPT="$TEMPLATES_DIR/review-doc.py"
            ;;
        *)
            echo "ERROR: Unknown template '$TEMPLATE'. Use: brief, review-doc" >&2
            exit 1
            ;;
    esac

    if [[ ! -f "$TEMPLATE_SCRIPT" ]]; then
        echo "ERROR: Template script not found: $TEMPLATE_SCRIPT" >&2
        exit 1
    fi

    "$PYTHON3" "$TEMPLATE_SCRIPT" "$INPUT_FILE" "$PDF_FILE"
else
    echo "ERROR: Unsupported file type '.$EXT'. Supported: .md, .pdf" >&2
    exit 1
fi

if [[ ! -f "$PDF_FILE" ]]; then
    echo "ERROR: Render produced no output at $PDF_FILE" >&2
    exit 1
fi

# ---- Push to reMarkable ----
echo "Pushing $PDF_FILE to reMarkable folder: $DEST_FOLDER"

# rmapi put syntax: rmapi put <local-file> [<remote-folder>]
# It creates the folder if it does not exist.
"$RMAPI" put "$PDF_FILE" "$DEST_FOLDER"

echo "Done. To verify: $RMAPI ls $DEST_FOLDER"

# Auto-register the destination tree in the annotation watcher's scope (design
# point 2026-07-23: watch coverage derives from what we push, never a hardcoded list).
TREES_FILE=~/.remarkable-watched-trees
TOP="/$(printf '%s' "$DEST_FOLDER" | sed 's|^/||;s|/.*||')"
grep -Fxq "$TOP" "$TREES_FILE" 2>/dev/null || echo "$TOP" >> "$TREES_FILE"
