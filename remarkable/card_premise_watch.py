#!/usr/bin/env python3
"""Read-only detector for a Decisions card whose premise went stale (mc-cxjp3).

The Decisions document is rendered once around 01:55 and then frozen for an
independent review receipt: the receipt is bound to the PDF and manifest by
hash, so re-rendering after review invalidates it (review-receipt.py). That
freeze is correct and stays untouched here. But the hours between render and
The reader answering are hours our own overnight work keeps running, and on
2026-09-15 the overnight session built and deployed the exact thing a frozen
card asked him about (a shared drop folder, mc-9ld2c.1). The reader answered a
question that our own action had already settled.

This tool does not change the document, the manifest, or the receipt. It is a
second, independent read: a snapshot of what each card's premise rested on at
one moment, and a later check of whether that premise still holds. The
property it protects: the reader is never asked to decide something our own action
already decided.

  snapshot - record each card's premise (row state, read-doc hash, linked
             ticket statuses) as true right now
  check    - re-read the same inputs and report any card whose premise has
             moved since the snapshot

A ticket is "linked" to a card only through an explicit marker: a
'Follow-up:' / 'Ticket:' / 'Tickets:' line in the read doc, or a 'Ticket:' /
'Tickets:' marker inside the card's own ask/description text. A ticket id
mentioned anywhere else in the prose (a historical reference, an unrelated
example) is never treated as a link, so that ticket's later status changes
cannot mark the card stale. A card with no explicit link is reported as
"no linked ticket" (checked only by its queue row and read-doc hash), never
silently treated as clean.

check exit codes: 0 clean, 2 missing manifest/snapshot, 3 something is
stale (wins over 4), 4 nothing is stale but a linked ticket could not be
checked (bd failed or returned no status, at snapshot time or at check
time; a snapshot-time failure is unable-to-check no matter what a later
check-time lookup returns, never silently clean). snapshot prints any
lookup failure it hits too, not only the file it writes.

check makes no network calls, sends nothing on the bus, and writes nothing to
disk. snapshot writes exactly one file: the snapshot itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rmw_config  # noqa: E402

_CONFIG = rmw_config.load()
MC = _CONFIG.content_root
# One shared id pattern for every use: a bare "mc-<id>" or a dotted
# sub-ticket "mc-<id>.<n>[.<n>...]". Used both to find ids anywhere (via
# TICKET_RE) and, unchanged, inside the marker patterns below, so a
# sentence-ending period after a dotted id (".../mc-uy3b.1.") is never
# swallowed into or truncated out of the id.
ID_PATTERN = r"mc-[a-z0-9]+(?:\.[0-9]+)*"
TICKET_RE = re.compile(ID_PATTERN)
# A comma/"and"-separated list of ids, e.g. "mc-a, mc-b and mc-c". Reused by
# both marker patterns below so "Ticket:"/"Tickets:" behave identically
# wherever they appear.
_ID_LIST_PATTERN = rf"{ID_PATTERN}(?:\s*(?:,|and)\s*{ID_PATTERN})*"
# A read-doc line that explicitly names linked tickets, matching the
# research follow-up gate's own convention ("Follow-up: mc-<id>"). Only the
# id list immediately after the marker is captured; prose beyond the list
# (or a line with no id at all, e.g. "Follow-up: none, because ...") is not.
LINE_MARKER_RE = re.compile(rf"^\s*(?:Follow-up|Ticket|Tickets):\s*({_ID_LIST_PATTERN})")
# An explicit "Ticket:" / "Tickets:" marker inside a card's ask/description
# prose. Only the id list immediately after the marker is captured; prose
# elsewhere in the card is never scanned.
TEXT_MARKER_RE = re.compile(rf"(?:Ticket|Tickets):\s*({_ID_LIST_PATTERN})")
SNAPSHOT_SCHEMA_VERSION = 1

BdLookup = Callable[[str], dict[str, Any]]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def default_manifest_path(date: str, archive_root: Path) -> Path:
    return Path(archive_root) / date / f"decisions-{date}.manifest.json"


def default_snapshot_path(date: str, archive_root: Path) -> Path:
    return Path(archive_root) / date / "card-premise-snapshot.json"


def extract_ticket_ids(text: str) -> list[str]:
    """Ticket ids mentioned in text, in first-seen order, de-duplicated."""
    seen: list[str] = []
    for match in TICKET_RE.finditer(text or ""):
        ticket_id = match.group(0)
        if ticket_id not in seen:
            seen.append(ticket_id)
    return seen


def active_row_keys(review_queue_text: str) -> set[str]:
    """Row keys currently printed under any '## Active ...' section.

    REVIEW-QUEUE.md has several '## Active ...' headings (Blocking /
    Production, Direction / Architecture, Triage) and many non-Active
    headings (Closed, Deferred by pen, Pen dispositions, ...). A card's row
    is live only while its row_key appears inside one of the Active
    sections.
    """
    row_re = re.compile(r"Row:\s*(\S+)")
    active: set[str] = set()
    in_active = False
    for line in review_queue_text.splitlines():
        if line.startswith("## "):
            in_active = line[3:].strip().startswith("Active")
            continue
        if in_active:
            for match in row_re.finditer(line):
                active.add(match.group(1))
    return active


def default_bd_lookup(ticket_id: str, *, repo_root: Path) -> dict[str, Any]:
    """Look up one ticket's live status via `bd show <id> --json` from repo_root."""
    try:
        result = subprocess.run(
            ["bd", "show", ticket_id, "--json"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": None, "error": f"bd show {ticket_id} failed to run: {exc}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return {
            "status": None,
            "error": f"bd show {ticket_id} exited {result.returncode}: {detail}",
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"status": None, "error": f"bd show {ticket_id} returned unparsable JSON: {exc}"}
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return {"status": None, "error": f"bd show {ticket_id} returned no issue"}
    return {"status": payload.get("status"), "id": payload.get("id")}


def _ids_from_marked_lines(text: str) -> list[str]:
    """Ticket ids on a 'Follow-up:' / 'Ticket:' / 'Tickets:' line (read docs)."""
    ids: list[str] = []
    for line in text.splitlines():
        match = LINE_MARKER_RE.match(line)
        if not match:
            continue
        for ticket_id in extract_ticket_ids(match.group(1)):
            if ticket_id not in ids:
                ids.append(ticket_id)
    return ids


def _ids_from_text_markers(text: str) -> list[str]:
    """Ticket ids after a 'Ticket:' / 'Tickets:' marker inside prose (ask/description)."""
    ids: list[str] = []
    for match in TEXT_MARKER_RE.finditer(text or ""):
        for ticket_id in extract_ticket_ids(match.group(1)):
            if ticket_id not in ids:
                ids.append(ticket_id)
    return ids


def _card_ticket_ids(card: dict[str, Any], repo_root: Path) -> list[str]:
    """Explicitly linked ticket ids only. A ticket id anywhere else in a
    card's prose, or anywhere else in its read doc, is a historical mention,
    not a link, and is ignored (mc-cxjp3, the cross-reviewer cross-review point 1)."""
    ticket_ids: list[str] = []
    for field in ("ask", "description"):
        for ticket_id in _ids_from_text_markers(str(card.get(field) or "")):
            if ticket_id not in ticket_ids:
                ticket_ids.append(ticket_id)

    read = card.get("read") or {}
    read_path = read.get("path")
    if read_path:
        full_path = Path(repo_root) / read_path
        if full_path.exists():
            try:
                doc_text = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                doc_text = ""
            for ticket_id in _ids_from_marked_lines(doc_text):
                if ticket_id not in ticket_ids:
                    ticket_ids.append(ticket_id)
    return ticket_ids


def build_snapshot(
    date: str,
    manifest: dict[str, Any],
    review_queue_text: str,
    repo_root: Path,
    bd_lookup: BdLookup,
) -> dict[str, Any]:
    active = active_row_keys(review_queue_text)
    cards_out: list[dict[str, Any]] = []
    for card in manifest.get("cards", []):
        row_key = card.get("row_key")
        read = card.get("read") or {}
        ticket_ids = _card_ticket_ids(card, repo_root)
        tickets = {ticket_id: bd_lookup(ticket_id) for ticket_id in ticket_ids}
        cards_out.append(
            {
                "card_id": card.get("card_id"),
                "row_key": row_key,
                "headline": card.get("headline"),
                "read_path": read.get("path"),
                "read_sha256": read.get("sha256"),
                "ticket_ids": ticket_ids,
                "tickets": tickets,
                "row_key_active_at_snapshot": bool(row_key) and row_key in active,
                "has_linked_ticket": bool(ticket_ids),
            }
        )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "date": date,
        "manifest_hash": manifest.get("manifest_hash"),
        "snapshotted_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cards": cards_out,
    }


NO_LINKED_TICKET_NOTE = "no linked ticket; checked by queue row and read doc only"


def check_snapshot(
    snapshot: dict[str, Any],
    review_queue_text: str,
    repo_root: Path,
    bd_lookup: BdLookup,
) -> dict[str, list[dict[str, Any]]]:
    """Compare the snapshot against current state.

    Returns {"stale": [...], "unable": [...], "no_linked_ticket": [...]}.
    A card lands in at most one of stale/unable (stale wins when a card has
    both a stale reason and an unable-to-check ticket); no_linked_ticket is
    informational and independent of the other two.
    """
    active = active_row_keys(review_queue_text)
    stale: list[dict[str, Any]] = []
    unable: list[dict[str, Any]] = []
    no_linked_ticket: list[dict[str, Any]] = []

    for card in snapshot.get("cards", []):
        reasons: list[str] = []
        unable_tickets: list[str] = []
        row_key = card.get("row_key")
        headline = card.get("headline")
        card_id = card.get("card_id")

        if row_key and card.get("row_key_active_at_snapshot") and row_key not in active:
            reasons.append(
                f"row {row_key} is no longer in REVIEW-QUEUE.md Active "
                "(moved to Closed or removed)"
            )

        read_path = card.get("read_path")
        if read_path:
            full_path = Path(repo_root) / read_path
            if not full_path.exists():
                reasons.append(f"read doc {read_path} no longer exists")
            else:
                current_sha = sha256_file(full_path)
                if current_sha != card.get("read_sha256"):
                    reasons.append(
                        f"read doc {read_path} sha256 changed since the card was snapshotted"
                    )

        ticket_ids = card.get("ticket_ids") or []
        if not ticket_ids:
            no_linked_ticket.append(
                {"card_id": card_id, "row_key": row_key, "headline": headline, "note": NO_LINKED_TICKET_NOTE}
            )

        tickets = card.get("tickets") or {}
        for ticket_id in ticket_ids:
            snapshot_info = tickets.get(ticket_id) or {}
            snapshot_status = snapshot_info.get("status")
            if snapshot_status is None:
                # The baseline itself is unknown (the snapshot-time bd show
                # failed or returned no status): there is nothing to compare
                # a current lookup against, so this is unable-to-check no
                # matter what check-time lookup returns, never "clean".
                detail = snapshot_info.get("error") or "no status returned"
                unable_tickets.append(f"{ticket_id} (snapshot lookup failed: {detail})")
                continue
            current_info = bd_lookup(ticket_id) or {}
            current_status = current_info.get("status")
            if current_status is None:
                detail = current_info.get("error") or "no status returned"
                unable_tickets.append(f"{ticket_id} ({detail})")
                continue
            if snapshot_status != current_status:
                reasons.append(
                    f"ticket {ticket_id} status changed from {snapshot_status!r} "
                    f"to {current_status!r}"
                )

        if reasons:
            stale.append(
                {"card_id": card_id, "row_key": row_key, "headline": headline, "reasons": reasons}
            )
        elif unable_tickets:
            unable.append(
                {
                    "card_id": card_id,
                    "row_key": row_key,
                    "headline": headline,
                    "unable_tickets": unable_tickets,
                }
            )

    return {"stale": stale, "unable": unable, "no_linked_ticket": no_linked_ticket}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_text_if_exists(path: Path) -> str:
    path = Path(path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=_CONFIG.decisions_archive,
    )
    parser.add_argument("--repo-root", type=Path, default=MC)
    parser.add_argument("--review-queue", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    snap = commands.add_parser(
        "snapshot", help="record each card's premise as true right now (writes one file)"
    )
    snap.add_argument("--date", required=True)
    snap.add_argument("--manifest", type=Path)
    snap.add_argument("--snapshot-path", type=Path)

    chk = commands.add_parser(
        "check", help="report any card whose premise has moved since the snapshot"
    )
    chk.add_argument("--date", required=True)
    chk.add_argument("--manifest", type=Path)
    chk.add_argument("--snapshot-path", type=Path)
    chk.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    review_queue_path = (
        Path(args.review_queue) if args.review_queue else repo_root / "REVIEW-QUEUE.md"
    )
    manifest_path = args.manifest or default_manifest_path(args.date, args.archive_root)
    snapshot_path = args.snapshot_path or default_snapshot_path(args.date, args.archive_root)

    def bd_lookup(ticket_id: str) -> dict[str, Any]:
        return default_bd_lookup(ticket_id, repo_root=repo_root)

    if args.command == "snapshot":
        if not manifest_path.exists():
            print(f"There is no manifest at {manifest_path}. Nothing snapshotted.", file=sys.stderr)
            return 2
        manifest = _load_json(manifest_path)
        review_queue_text = _read_text_if_exists(review_queue_path)
        snapshot = build_snapshot(args.date, manifest, review_queue_text, repo_root, bd_lookup)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        print(f"wrote snapshot: {snapshot_path}")
        for card in snapshot.get("cards", []):
            if not card.get("has_linked_ticket"):
                print(f"{card['card_id']} row={card['row_key']}: {NO_LINKED_TICKET_NOTE}")
                continue
            for ticket_id, info in (card.get("tickets") or {}).items():
                if info.get("status") is None:
                    detail = info.get("error") or "no status returned"
                    print(
                        f"{card['card_id']} row={card['row_key']}: "
                        f"UNABLE TO CHECK {ticket_id} (snapshot lookup failed: {detail})"
                    )
        return 0

    # check
    missing: list[str] = []
    if not manifest_path.exists():
        missing.append(f"There is no manifest at {manifest_path}.")
    if not snapshot_path.exists():
        missing.append(
            f"There is no snapshot at {snapshot_path}. Run "
            f"'snapshot --date {args.date}' first."
        )
    if missing:
        for line in missing:
            print(line, file=sys.stderr)
        return 2

    try:
        snapshot = _load_json(snapshot_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"The snapshot at {snapshot_path} could not be read: {exc}", file=sys.stderr)
        return 2

    review_queue_text = _read_text_if_exists(review_queue_path)
    result = check_snapshot(snapshot, review_queue_text, repo_root, bd_lookup)
    stale = result["stale"]
    unable = result["unable"]
    no_linked_ticket = result["no_linked_ticket"]

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for entry in no_linked_ticket:
            print(f"{entry['card_id']} row={entry['row_key']}: {entry['note']}")
        for entry in unable:
            tickets = ", ".join(entry["unable_tickets"])
            print(
                f"UNABLE TO CHECK {entry['card_id']} row={entry['row_key']} "
                f"{entry['headline']!r}: {tickets}"
            )
        for entry in stale:
            reasons = "; ".join(entry["reasons"])
            print(
                f"STALE {entry['card_id']} row={entry['row_key']} "
                f"{entry['headline']!r}: {reasons}"
            )
        if not stale and not unable:
            print("No card's premise has changed since the snapshot.")

    if stale:
        return 3
    if unable:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
