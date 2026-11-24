#!/usr/bin/env python3
"""Is a registered Decisions document's ink already harvested? (mc-9jd4t)

On 2026-09-13 the reader penned seven answers on the day's Decisions document and
then deleted it from the tablet, because he found the writing unreadable. The
delivery script tried to archive the registered document, could not fetch it,
and aborted with a generic "could not archive". The answers were sitting in
the tablet's trash, recorded nowhere, and the rebuilt document was about to
re-ask four of them.

A registered document is now never replaced or abandoned until its ink has
been harvested. This says whether a harvest receipt exists and actually
belongs to the document being replaced.

Exit 0 when the receipt is valid for this manifest, 1 otherwise. It never
writes anything and it never guesses: an unreadable or mismatched receipt is
a "not harvested" answer, because the cost of being wrong is a re-asked
question.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL = "rmw-decisions-harvest-v1"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: check_harvest_receipt.py <harvest-receipt.json> "
            "<registry.json> <device-path>",
            file=sys.stderr,
        )
        return 2
    receipt_path, registry_path, device_path = (
        Path(argv[1]), Path(argv[2]), argv[3]
    )
    if not receipt_path.is_file():
        return 1
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 1
    if not isinstance(receipt, dict):
        return 1
    if receipt.get("tool") != TOOL:
        return 1
    ink_hash = receipt.get("ink_hash")
    if not isinstance(ink_hash, str) or not ink_hash.strip():
        return 1
    page_id = receipt.get("page_id")
    if not isinstance(page_id, str) or not page_id.strip():
        return 1
    # The receipt has to name the document that is actually being REPLACED,
    # which is the one the registry records at this device path, not the new
    # one being pushed over it. Those page ids differ on every re-render, and
    # comparing against the new manifest would accept a harvest of nothing.
    registered = (registry.get("pages") or {}).get(device_path) or {}
    if not registered.get("page_id"):
        return 1
    if page_id != registered["page_id"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
