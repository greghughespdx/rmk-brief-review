#!/usr/bin/env python3
"""Example hook: send a message to an HTTP agent message bus.

This is the delivery the workflow was built around: a durable message bus
that a coordinating agent reads. The agent pulls the annotated document,
vision-reads anything the deterministic resolver could not, and acts on clear
answers. Any service with an authenticated "send" endpoint works the same way.

Wire it as any hook that takes (subject, body, ...):

  [hooks]
  alert = ["python3", "/path/to/hooks/examples/agent-bus-send.py"]
  on_annotation = ["python3", "/path/to/hooks/examples/agent-bus-send.py"]

Environment:
  AGENTBUS_SERVICE_URL   base URL, for example http://bus.example.internal:8080
  AGENTBUS_TOKEN         sender token (keep it in a secret store; never commit it)
  AGENTBUS_RECIPIENTS    comma-separated recipient ids (default: orchestrator)

Exit 0 only when the bus acknowledges the message with a message id, so the
caller can restore its baseline and retry on any failure.
"""
import datetime
import json
import os
import sys
import urllib.request


def main(argv):
    if len(argv) < 3:
        print("usage: agent-bus-send.py <subject> <body> [slug-or-path ...]", file=sys.stderr)
        return 2
    subject, body = argv[1], argv[2]
    url = os.environ.get("AGENTBUS_SERVICE_URL", "").rstrip("/")
    token = os.environ.get("AGENTBUS_TOKEN", "")
    if not url or not token:
        print("agent-bus-send: AGENTBUS_SERVICE_URL and AGENTBUS_TOKEN are required", file=sys.stderr)
        return 1
    raw = os.environ.get("AGENTBUS_RECIPIENTS") or os.environ.get("BUS_ALERT_RECIPIENTS") or "orchestrator"
    recipients = [r for r in raw.split(",") if r and r != "operators"] or ["orchestrator"]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    slug = argv[3] if len(argv) > 3 else "remarkable"
    payload = {
        "recipients": recipients,
        "payload_type": "a2a.Message",
        "idempotency_key": f"{slug}-{stamp}",
        "payload": {"subject": subject, "parts": [{"kind": "text", "text": body}]},
    }
    req = urllib.request.Request(
        url + "/v1/send",
        data=json.dumps(payload).encode(),
        headers={"x-agent-token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not result.get("bus_message_id"):
            raise ValueError("delivery was not acknowledged")
    except Exception as exc:  # noqa: BLE001 - report every failure the same way
        print(f"agent-bus-send: send failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
