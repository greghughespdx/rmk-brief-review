#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("facts_json")
parser.add_argument("--queue", default="")
parser.add_argument("--row-key", default="")
parser.add_argument("--render-date", required=True)
args = parser.parse_args()
path = Path(args.facts_json)
payload = json.loads(path.read_text())
if payload.get("row_key") and payload["row_key"] != args.row_key:
    print(f"row key mismatch: expected {payload['row_key']} got {args.row_key}", file=sys.stderr)
    sys.exit(1)
if payload.get("render_date") and payload["render_date"] != args.render_date:
    print(f"render date mismatch: expected {payload['render_date']} got {args.render_date}", file=sys.stderr)
    sys.exit(1)
if payload.get("queue_contains"):
    queue_text = Path(args.queue).read_text() if args.queue else ""
    if payload["queue_contains"] not in queue_text:
        print(f"queue text missing {payload['queue_contains']!r}", file=sys.stderr)
        sys.exit(1)
expected = payload.get("expected_counts")
actual = payload.get("actual_counts", expected)
if expected != actual:
    print(f"report counts differ: expected {expected} got {actual}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
