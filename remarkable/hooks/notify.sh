#!/usr/bin/env bash
# Reader notification: notify.sh <message> [image-path]
#
# Used for the one-line morning outcome and for same-day questions about an
# unclear pen mark. Forwards to hooks.notify (RMW_HOOKS_NOTIFY). With no hook
# configured it logs one line to stderr and exits 3 (not delivered),
# so the caller never records the message as sent.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RMW_RENDER_PYTHON:-python3}"
exec "$PY" "$DIR/rmw_config.py" hook notify "$@"
