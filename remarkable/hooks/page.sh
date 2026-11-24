#!/usr/bin/env bash
# Urgent operator push: page.sh --title T --body B --level L --source S
#
# Used by the daily pen-chain probe when the chain from pen mark to recorded
# answer is broken. Forwards to hooks.page (RMW_HOOKS_PAGE), for example a
# phone push service. With no hook configured it logs one line to stderr and
# exits 3 (not delivered).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RMW_RENDER_PYTHON:-python3}"
exec "$PY" "$DIR/rmw_config.py" hook page "$@"
