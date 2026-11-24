#!/usr/bin/env bash
# Example hook for any hook name: append every call to a log file.
# Useful while setting up, to see exactly what each hook would send.
#   [hooks]
#   alert = ["/path/to/hooks/examples/log-to-file.sh"]
set -euo pipefail
log="${HOOK_LOG:-$HOME/Library/Logs/remarkable-workflow-hooks.log}"
mkdir -p "$(dirname "$log")"
{ printf '[%s]' "$(date '+%F %T')"; printf ' %q' "$@"; printf '\n'; } >> "$log"
