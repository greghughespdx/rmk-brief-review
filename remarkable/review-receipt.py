#!/usr/bin/env python3
"""The final-artifact review gate for the Decisions document (mc-iacbw, s6).

The reader asked for a second read of every card, every morning, by something that
did not write the card, against three tests: complete context, answer forms
that fit the questions, and plain English written for him (interview answer 6).
The cross-reviewer's review found the rule existed only as prose: the generator required
no receipt, no reviewer identity, and no tie to the artifact that actually
gets pushed, so a card could pass review and ship in a form the review never
saw.

This is that rule as a gate. The receipt names the reviewer, the exact PDF and
the exact manifest by hash, and answers the three tests per card. The push
refuses without it, and any edit to the document after the review invalidates
the receipt by hash, because the hash no longer matches.

  check  - validate a receipt against the PDF and manifest being pushed
  write  - the reviewer's helper: compute the hashes and record the verdicts

The reviewer writes the receipt. This tool never invents a verdict: `write`
records exactly what the reviewer passes it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rmw_config  # noqa: E402

MC = rmw_config.PROJECT_ROOT
TESTS = ("complete_context", "forms_fit", "plain_english")
TEST_NAMES = {
    "complete_context": "the complete-context test",
    "forms_fit": "the answer-form test (the boxes fit the questions)",
    "plain_english": "the plain-English test",
}
RECEIPT_NAME = "review-receipt.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _archive_dir(date: str, archive_root: Path) -> Path:
    return Path(archive_root) / date


def default_paths(date: str, archive_root: Path) -> dict[str, Path]:
    directory = _archive_dir(date, archive_root)
    return {
        "pdf": directory / f"decisions-{date}.pdf",
        "manifest": directory / f"decisions-{date}.manifest.json",
        "receipt": directory / RECEIPT_NAME,
    }


def check_receipt(
    receipt_path: Path,
    pdf_path: Path,
    manifest_path: Path,
    producer: str = "",
) -> list[str]:
    """Return the reasons this document may not be pushed (empty = it may)."""
    problems: list[str] = []
    if not Path(receipt_path).exists():
        return [
            f"There is no review receipt at {receipt_path}. Nothing is pushed "
            "until a reviewer who did not write the cards has read the "
            "rendered document."
        ]
    try:
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"The review receipt could not be read ({exc})."]
    if not isinstance(receipt, dict):
        return ["The review receipt is not in the expected shape."]

    reviewer = str(receipt.get("reviewer") or "").strip()
    if not reviewer:
        problems.append("The review receipt does not say who reviewed it.")
    elif producer and reviewer.lower() == producer.strip().lower():
        problems.append(
            f"The review was written by {reviewer}, which is who produced the "
            "document. The reviewer has to be someone else."
        )
    if not str(receipt.get("reviewed_at") or "").strip():
        problems.append("The review receipt does not say when it was reviewed.")

    try:
        manifest_bytes = Path(manifest_path).read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return problems + [f"The manifest could not be read ({exc})."]
    if not isinstance(manifest, dict):
        return problems + ["The manifest is not in the expected shape."]

    if not Path(pdf_path).exists():
        problems.append(f"The document is missing at {pdf_path}.")
    else:
        actual_pdf = sha256_file(pdf_path)
        if receipt.get("pdf_sha256") != actual_pdf:
            problems.append(
                "The document has changed since it was reviewed, so the review "
                "no longer covers what would be pushed. Re-render, review "
                "again, then push."
            )
    if receipt.get("manifest_hash") != manifest.get("manifest_hash"):
        problems.append(
            "The manifest has changed since it was reviewed, so the review no "
            "longer covers what would be pushed."
        )
    if not receipt.get("manifest_sha256"):
        problems.append(
            "The review receipt does not include a hash of the manifest file. "
            "Review again before pushing."
        )
    elif receipt["manifest_sha256"] != hashlib.sha256(manifest_bytes).hexdigest():
        problems.append(
            "The manifest has changed since it was reviewed, so the review no "
            "longer covers what would be pushed."
        )

    cards = receipt.get("cards")
    if not isinstance(cards, list):
        return problems + ["The review receipt records no cards."]
    reviewed = {}
    for card in cards:
        if not isinstance(card, dict) or not card.get("card_id"):
            problems.append("A card in the review receipt has no card id.")
            continue
        reviewed[card["card_id"]] = card
    expected = [card["card_id"] for card in manifest.get("cards", [])]
    for card_id in expected:
        card = reviewed.get(card_id)
        headline = next(
            (
                entry.get("headline", card_id)
                for entry in manifest.get("cards", [])
                if entry.get("card_id") == card_id
            ),
            card_id,
        )
        if card is None:
            problems.append(f"No card in the receipt reviewed {headline!r}.")
            continue
        for test in TESTS:
            if str(card.get(test) or "").strip().lower() != "yes":
                note = str(card.get("note") or "").strip()
                detail = f" The reviewer wrote: {note}" if note else ""
                problems.append(
                    f"The card {headline!r} did not pass {TEST_NAMES[test]}."
                    + detail
                )
    for card_id in reviewed:
        if card_id not in expected:
            problems.append(
                f"The receipt reviews {card_id}, which is not in this document."
            )
    return problems


def _parse_verdict(raw: str) -> dict[str, str]:
    """card_id:yes,yes,yes[:note] as the reviewer types it."""
    parts = raw.split(":", 2)
    if len(parts) < 2:
        raise SystemExit(
            f"A verdict must be written card_id:yes,yes,yes[:note], not {raw!r}."
        )
    answers = [value.strip().lower() for value in parts[1].split(",")]
    if len(answers) != len(TESTS) or any(
        answer not in ("yes", "no") for answer in answers
    ):
        raise SystemExit(
            f"The verdict for {parts[0]} needs three answers, each yes or no, "
            "for complete context, answer forms, and plain English."
        )
    card = {"card_id": parts[0].strip()}
    card.update(dict(zip(TESTS, answers)))
    card["note"] = parts[2].strip() if len(parts) > 2 else ""
    return card


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path,
                        default=Path(os.environ.get("REMARKABLE_TODAY_ARCHIVE") or rmw_config.load().decisions_archive))
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="refuse a document that was not reviewed")
    check.add_argument("--date", required=True)
    check.add_argument("--pdf", type=Path)
    check.add_argument("--manifest", type=Path)
    check.add_argument("--receipt", type=Path)
    check.add_argument("--producer", default="")

    write = commands.add_parser("write", help="record a reviewer's verdicts")
    write.add_argument("--date", required=True)
    write.add_argument("--reviewer", required=True)
    write.add_argument("--pdf", type=Path)
    write.add_argument("--manifest", type=Path)
    write.add_argument("--receipt", type=Path)
    write.add_argument("--verdict", action="append", default=[],
                       help="card_id:yes,yes,yes[:note], once per card")

    args = parser.parse_args()
    defaults = default_paths(args.date, args.archive_root)
    pdf = args.pdf or defaults["pdf"]
    manifest = args.manifest or defaults["manifest"]
    receipt = args.receipt or defaults["receipt"]

    if args.command == "check":
        problems = check_receipt(receipt, pdf, manifest, args.producer)
        if problems:
            print("This document was NOT pushed. Why:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 4
        print(f"review receipt valid: {receipt}")
        return 0

    cards = [_parse_verdict(raw) for raw in args.verdict]
    if not cards:
        raise SystemExit(
            "A receipt with no cards reviews nothing. Pass one --verdict per "
            "card in the document."
        )
    manifest_bytes = Path(manifest).read_bytes()
    payload = {
        "pdf_sha256": sha256_file(pdf),
        "manifest_hash": json.loads(manifest_bytes).get("manifest_hash"),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "reviewer": args.reviewer,
        "reviewed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cards": cards,
    }
    Path(receipt).parent.mkdir(parents=True, exist_ok=True)
    Path(receipt).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
