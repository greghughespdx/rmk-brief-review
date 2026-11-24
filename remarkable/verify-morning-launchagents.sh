#!/usr/bin/env bash
# Read-only activation gate for the two morning LaunchAgents.

set -uo pipefail

LAUNCHCTL="${LAUNCHCTL:-launchctl}"
DOMAIN="${LAUNCHD_DOMAIN:-gui/$(id -u)}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Load the config into RMW_* variables. A copy of this script outside the
# repository (a test harness) falls back to RMW_PROJECT_ROOT, then to the
# built-in defaults below.
if [ -f "$DIR/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$DIR/rmw-env.sh"
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
PREFIX="${RMW_SCHEDULE_LABEL_PREFIX:-com.example.remarkable-workflow}"
DASHBOARD_LABEL="${MB_DASHBOARD_LABEL:-$PREFIX.morning-delivery}"
DEADMAN_LABEL="${MB_DEADMAN_LABEL:-$PREFIX.outcome-deadman}"
DASHBOARD_PLIST="${MB_DASHBOARD_PLIST:-$HOME/Library/LaunchAgents/$DASHBOARD_LABEL.plist}"
DEADMAN_PLIST="${MB_DEADMAN_PLIST:-$HOME/Library/LaunchAgents/$DEADMAN_LABEL.plist}"

require_plist_value() {
  local plist="$1" key="$2" expected="$3" actual
  actual="$(plutil -extract "$key" raw -o - "$plist" 2>/dev/null)" || return 1
  [ "$actual" = "$expected" ]
}

verify_loaded_job() {
  local label="$1" flag="$2" report
  report="$($LAUNCHCTL print "$DOMAIN/$label" 2>/dev/null)" || return 1
  printf '%s\n' "$report" | grep -Eq "${flag}[[:space:]]*=>[[:space:]]*1[[:space:]]*$"
}

require_plist_value "$DASHBOARD_PLIST" EnvironmentVariables.MB_ALLOW_OUTBOUND 1 || {
  echo "Morning dashboard plist does not enable its outbound guard." >&2
  exit 1
}
require_plist_value "$DEADMAN_PLIST" EnvironmentVariables.MB_DEADMAN_ALLOW_OUTBOUND 1 || {
  echo "Morning dead-man plist does not enable its agent alert." >&2
  exit 1
}
verify_loaded_job "$DASHBOARD_LABEL" MB_ALLOW_OUTBOUND || {
  echo "Morning dashboard is not loaded with outbound delivery enabled." >&2
  exit 1
}
verify_loaded_job "$DEADMAN_LABEL" MB_DEADMAN_ALLOW_OUTBOUND || {
  echo "Morning dead-man is not loaded with agent alerts enabled." >&2
  exit 1
}

echo "Morning delivery LaunchAgents are loaded with their explicit outbound gates enabled."
