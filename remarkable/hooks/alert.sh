#!/usr/bin/env bash
# Operator alert: alert.sh <subject> <body> [slug]
#
# Every failure path in the workflow calls this one script. It forwards to the
# command configured as hooks.alert in the config file (RMW_HOOKS_ALERT in the
# environment). With no hook configured it logs one line to stderr and exits 0,
# so an unconfigured install never fails because nobody is listening.
# See hooks/examples/ for working hook commands (an HTTP message bus, a push
# notification service, email).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RMW_RENDER_PYTHON:-python3}"
exec "$PY" "$DIR/rmw_config.py" hook alert "$@"
