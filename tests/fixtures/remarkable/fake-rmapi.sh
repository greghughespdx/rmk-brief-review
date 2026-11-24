#!/bin/sh
set -eu

if [ "${1:-}" != "get" ] || [ -z "${REMARKABLE_FIXTURE_ARCHIVE:-}" ]; then
  echo "fixture rmapi accepts: get PATH" >&2
  exit 2
fi

cp "$REMARKABLE_FIXTURE_ARCHIVE" ./today-local-fixture.rmdoc
