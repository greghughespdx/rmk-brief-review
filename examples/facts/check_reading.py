#!/usr/bin/env python3
"""Example Facts verifier: re-check a card's dated facts at render time.

A card that states dates, counts or "today"-style facts must name a verifier
with "Facts: <path> [args...]" just before "Row:". The renderer runs it every
time it renders, from the content root, and appends
"--queue <file> --row-key <key> --render-date YYYY-MM-DD". A nonzero exit holds
the card off the document, with the verifier's message on the cover page.

This one checks two things: every quoted phrase still appears in the card's
reading document (so the card and its evidence agree), and, with --before,
that the render date is still before the deadline the card is about. A real
verifier would ask the system of record instead (a calendar, an order page,
a monitoring API).

  check_reading.py <reading.md> [--expect TEXT ...] [--before YYYY-MM-DD]
"""
import argparse
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("reading")
parser.add_argument("--expect", action="append", default=[])
parser.add_argument("--before")
parser.add_argument("--queue", default="")
parser.add_argument("--row-key", default="")
parser.add_argument("--render-date", required=True)
args = parser.parse_args()

text = Path(args.reading).read_text(encoding="utf-8")
missing = [phrase for phrase in args.expect if phrase not in text]
if missing:
    print(f"{args.row_key}: the reading no longer says {missing!r}", file=sys.stderr)
    sys.exit(1)
if args.before and args.render_date >= args.before:
    print(f"{args.row_key}: the deadline {args.before} has passed; rewrite the card", file=sys.stderr)
    sys.exit(1)
print(f"{args.row_key}: facts current on {args.render_date}")
