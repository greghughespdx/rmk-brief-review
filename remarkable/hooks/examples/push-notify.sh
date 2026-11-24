#!/usr/bin/env bash
# Example hook: a phone push through an ntfy-style HTTP push service.
#
# Works as hooks.notify (one message argument) or hooks.page
# (--title T --body B --level L --source S).
#
#   [hooks]
#   notify = ["/path/to/hooks/examples/push-notify.sh"]
#   page   = ["/path/to/hooks/examples/push-notify.sh"]
#
# Environment: PUSH_URL (for example https://ntfy.sh/<your-private-topic>),
# PUSH_TOKEN (optional bearer token).
set -euo pipefail
: "${PUSH_URL:?set PUSH_URL to your push topic URL}"
title="reMarkable workflow"; body=""; priority="default"
if [ "${1:-}" = "--title" ]; then
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --title) title="$2"; shift 2 ;;
      --body) body="$2"; shift 2 ;;
      --level) [ "$2" = critical ] && priority="urgent"; shift 2 ;;
      *) shift ;;
    esac
  done
else
  body="${1:-}"
fi
auth=()
[ -n "${PUSH_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $PUSH_TOKEN")
curl -fsS "${auth[@]}" -H "Title: $title" -H "Priority: $priority" --data-binary "$body" "$PUSH_URL" >/dev/null
