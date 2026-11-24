#!/usr/bin/env bash
# Example hooks.notify: send the message to yourself with Messages.app.
#
#   [hooks]
#   notify = ["/path/to/hooks/examples/macos-imessage.sh"]
#
# Environment: IMESSAGE_TO (your own phone number or Apple ID email).
# The Mac running the workflow must be signed in to Messages, and the first
# run asks for Automation permission for the calling process.
set -euo pipefail
: "${IMESSAGE_TO:?set IMESSAGE_TO to the address to message}"
msg="${1:?message required}"
/usr/bin/osascript - "$IMESSAGE_TO" "$msg" <<'OSA'
on run argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send (item 2 of argv) to participant (item 1 of argv) of svc
  end tell
end run
OSA
