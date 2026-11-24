#!/usr/bin/env python3
"""Consume schema-2 Decisions documents without losing partial answers."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import consume_decisions as cd
import question_state as qs
import today_page as tp

QUESTION_RE = re.compile(r"\((\d+):([a-z0-9_]+)\)")
# The key may be followed by a period, as the queue rows write it ("Row: key. Read: ...");
# the renderer accepts both forms, so the consumer must too (found on the first live
# v3 round trip, 2026-09-02).
ROW_KEY_RE = re.compile(r"(?:^|\s)Row:\s*([a-z0-9_]+)\.?(?=\s+Read:)")
# Same-day questions to the reader go through the configured notify hook.
IMESSAGE_ALERT = Path(__file__).resolve().parent / "hooks" / "notify.sh"


class DecisionConsumerV3Error(RuntimeError):
    """A schema-2 document cannot be consumed safely."""


@dataclass(frozen=True)
class ActiveRow:
    item_id: str
    row_key: str
    title: str
    ask: str
    question_keys: tuple[str, ...]


@dataclass
class ConsumeResult:
    page_id: str
    changed_entry_ids: list[str]
    closed_item_ids: list[str]
    kept_item_ids: list[str]
    clarification_entry_ids: list[str]
    notifications_sent: list[str]
    notifications_failed: list[str]
    applied: bool
    # mc-z5hy3: handwriting the resolver captured but no human has read yet.
    notes: list[dict[str, Any]] = field(default_factory=list)
    # mc-9sy2v: the rows a receipt is due for in THIS document, dry run or
    # not, so a dry run can show what it would write and an apply run can
    # show what it did (or found already recorded).
    answered_item_ids: list[str] = field(default_factory=list)


def question_entry_id(item_id: str, question_key: str) -> str:
    return hashlib.sha256(f"{item_id}\0{question_key}".encode("ascii")).hexdigest()[:12]


def question_set_hash(question_keys: Iterable[str]) -> str:
    return hashlib.sha256(
        "\0".join(sorted(question_keys)).encode("ascii")
    ).hexdigest()


def active_rows(path: Path) -> dict[str, ActiveRow]:
    rows: dict[str, ActiveRow] = {}
    active = False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            active = line.startswith("## Active")
            continue
        if not active:
            continue
        cells = tp._split_row(line)
        if cells is None or len(cells) < 4 or cells[0] == "Item":
            continue
        title = tp.clean_markdown(cells[0])
        # The renderer rejoins the Ask cells, including a Specimen token pipe.
        ask = "|".join(cells[3:]).strip()
        row_keys = ROW_KEY_RE.findall(ask)
        if len(row_keys) != 1:
            raise DecisionConsumerV3Error(
                f"active row {title!r} has a missing or repeated Row key"
            )
        row_key = row_keys[0]
        item_id = hashlib.sha256(row_key.encode("ascii")).hexdigest()[:12]
        if item_id in rows:
            raise DecisionConsumerV3Error(
                f"active row {title!r} repeats Row key {row_key!r}"
            )
        decision_text = ask.split("Decide:", 1)[1] if "Decide:" in ask else ""
        matches = QUESTION_RE.findall(decision_text)
        keys = tuple(key for _, key in matches)
        if not keys or len(keys) != len(set(keys)):
            raise DecisionConsumerV3Error(
                f"active row {title!r} has missing or duplicate question keys"
            )
        rows[item_id] = ActiveRow(item_id, row_key, title, ask, keys)
    return rows


def _validate_document(
    manifest: dict[str, Any],
    resolution: dict[str, Any],
    rows: dict[str, ActiveRow],
    current_state: dict[str, Any],
) -> None:
    if manifest.get("schema_version") != 2 or resolution.get("schema_version") != 2:
        raise DecisionConsumerV3Error("v3 consumption requires schema version 2")
    if resolution.get("page_id") != manifest.get("page_id"):
        raise DecisionConsumerV3Error("resolution page_id does not match the manifest")
    if resolution.get("layout_version") != manifest.get("layout_version"):
        raise DecisionConsumerV3Error("resolution layout does not match the manifest")
    manifest_entries = manifest.get("entries")
    decisions = resolution.get("decisions")
    cards = manifest.get("cards")
    if not isinstance(manifest_entries, list) or not isinstance(decisions, list) or not isinstance(cards, list):
        raise DecisionConsumerV3Error("manifest or resolution entries are missing")
    by_entry = {entry.get("entry_id"): entry for entry in manifest_entries if isinstance(entry, dict)}
    by_decision = {entry.get("entry_id"): entry for entry in decisions if isinstance(entry, dict)}
    if len(by_entry) != len(manifest_entries) or set(by_entry) != set(by_decision):
        raise DecisionConsumerV3Error(
            "resolution must contain exactly one status for every manifest entry"
        )
    for entry_id, entry in by_entry.items():
        expected = question_entry_id(entry["item_id"], entry["question_key"])
        if entry_id != expected:
            raise DecisionConsumerV3Error(
                f"entry {entry_id!r} does not match its stable question key"
            )
        decision = by_decision[entry_id]
        for field in (
            "item_id",
            "card_id",
            "question_key",
            "decision_number",
            "label",
            "answer_type",
            "page",
        ):
            if decision.get(field) != entry.get(field):
                raise DecisionConsumerV3Error(
                    f"resolution entry {entry_id!r} disagrees on {field}"
                )
        _validate_decision_evidence(entry, decision)
    cards_by_id = {
        card.get("card_id"): card for card in cards if isinstance(card, dict)
    }
    if len(cards_by_id) != len(cards):
        raise DecisionConsumerV3Error("manifest cards have missing or repeated ids")
    entries_by_card: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest_entries:
        entries_by_card.setdefault(entry["card_id"], []).append(entry)
    if set(entries_by_card) != set(cards_by_id):
        raise DecisionConsumerV3Error(
            "manifest cards and entries do not describe the same card set"
        )
    for card in cards:
        card_entries = entries_by_card[card["card_id"]]
        if any(entry["item_id"] != card.get("item_id") for entry in card_entries):
            raise DecisionConsumerV3Error(
                f"manifest card {card.get('headline')!r} mixes queue rows"
            )
        item_id = card.get("item_id")
        row = rows.get(item_id)
        if row is None:
            # The row left Active. That is fine only when this very page already
            # recorded every one of the card's questions (terminal, or deferred
            # and then parked by hand, mc-q2pgy): a re-detection of the same
            # document then has nothing left to record for this card.
            already_recorded_here = all(
                current_state["entries"].get(entry["entry_id"], {}).get("source")
                == resolution["page_id"]
                and current_state["entries"][entry["entry_id"]].get("state")
                in (qs.TERMINAL_STATES | {"deferred"})
                for entry in card_entries
            )
            if already_recorded_here:
                continue
            raise DecisionConsumerV3Error(
                f"manifest card {card.get('headline')!r} is no longer Active"
            )
        keys = tuple(entry["question_key"] for entry in card_entries)
        invented = set(keys) - set(row.question_keys)
        missing = set(row.question_keys) - set(keys)
        missing_nonterminal = [
            key
            for key in missing
            if current_state["entries"].get(
                question_entry_id(row.item_id, key), {}
            ).get("state")
            not in qs.TERMINAL_STATES
        ]
        if invented or missing_nonterminal:
            raise DecisionConsumerV3Error(
                f"manifest card {card.get('headline')!r} omits nonterminal or invents questions"
            )
        expected_hash = question_set_hash(row.question_keys)
        if card.get("question_set_hash") != expected_hash:
            raise DecisionConsumerV3Error(
                f"manifest card {card.get('headline')!r} has a stale question set"
            )


def _validate_decision_evidence(
    entry: dict[str, Any], decision: dict[str, Any]
) -> None:
    """Prove the claimed status follows from the manifest controls and ink."""
    status = decision.get("status")
    marked = decision.get("marked_choices")
    if status not in qs.RESOLUTION_TO_STATE or not isinstance(marked, list):
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has an invalid status or marks"
        )
    if any(not isinstance(value, str) for value in marked) or len(marked) != len(set(marked)):
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has invalid marked choices"
        )
    boxes = entry.get("boxes")
    if not isinstance(boxes, list):
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has no answer controls"
        )
    box_choices = {
        box.get("choice")
        for box in boxes
        if isinstance(box, dict) and isinstance(box.get("choice"), str)
    }
    if len(box_choices) != len(boxes):
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has invalid answer controls"
        )
    answer_type = entry.get("answer_type")
    if answer_type == "yes_no":
        expected_choices = {"YES", "NO", "DEFER", "DISCARD"}
    elif answer_type == "write_in":
        expected_choices = {"DEFER", "DISCARD"}
    elif answer_type == "choice":
        options = entry.get("options")
        if not isinstance(options, list):
            raise DecisionConsumerV3Error(
                f"resolution entry {entry['entry_id']!r} has no choice options"
            )
        expected_choices = {
            option.get("letter")
            for option in options
            if isinstance(option, dict) and isinstance(option.get("letter"), str)
        } | {"DEFER", "DISCARD"}
        if len(expected_choices) != len(options) + 2:
            raise DecisionConsumerV3Error(
                f"resolution entry {entry['entry_id']!r} has invalid choice options"
            )
    else:
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has an invalid answer type"
        )
    if box_choices != expected_choices:
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} controls do not match its answer type"
        )
    if not set(marked) <= box_choices:
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} names a choice not on the card"
        )
    write_in = decision.get("write_in")
    has_writing = isinstance(write_in, dict) and bool(write_in.get("stroke_ids"))
    if has_writing and not isinstance(entry.get("write_in"), dict):
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has writing without a write-in line"
        )
    if write_in is not None and not has_writing:
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} has invalid write-in evidence"
        )
    answer_marks = [value for value in marked if value not in {"DEFER", "DISCARD"}]
    conflicting = (
        len(marked) > 1
        or (("DEFER" in marked or "DISCARD" in marked) and has_writing)
    )
    if conflicting:
        expected_status, expected_choice = "needs_review", None
    elif marked == ["DEFER"]:
        expected_status, expected_choice = "deferred", "DEFER"
    elif marked == ["DISCARD"]:
        expected_status, expected_choice = "discarded", "DISCARD"
    elif len(answer_marks) == 1:
        expected_status, expected_choice = "answered", answer_marks[0]
    elif has_writing:
        expected_status, expected_choice = "answered", None
    else:
        expected_status, expected_choice = "unanswered", None
    choice = decision.get("choice")
    # For needs_review the resolver reports the marked box it saw (for example
    # DEFER with handwriting beside it) while the consumer expected None; both
    # are the same evidence, so either spelling is accepted (found on the first
    # live v3 round trip, 2026-09-02, card-005 effort_level).
    choice_ok = (
        choice == expected_choice
        or (expected_status == "needs_review" and (choice is None or choice in marked))
    )
    if status != expected_status or not choice_ok:
        raise DecisionConsumerV3Error(
            f"resolution entry {entry['entry_id']!r} status does not match its marks"
        )


def _rewrite_closed_disposition(
    path: Path, row: ActiveRow, disposition: str
) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    closed = next(
        (index for index, line in enumerate(lines) if line.startswith("## Closed")),
        None,
    )
    if closed is None:
        raise DecisionConsumerV3Error("Closed table is missing after row closure")
    separator = next(
        (
            index
            for index in range(closed + 1, min(closed + 8, len(lines)))
            if lines[index].startswith("|---")
        ),
        None,
    )
    if separator is None or separator + 1 >= len(lines):
        raise DecisionConsumerV3Error("Closed table is malformed after row closure")
    cells = tp._split_row(lines[separator + 1])
    if cells is None or tp.clean_markdown(cells[0]) != tp.clean_markdown(row.title):
        raise DecisionConsumerV3Error("the row just closed is not the expected row")
    lines[separator + 1] = "| " + " | ".join(
        [
            tp.escape_table_cell(cells[0]),
            tp.escape_table_cell(cells[1]),
            tp.escape_table_cell(f"{disposition} Original ask: {row.ask}"),
        ]
    ) + " |"
    tp.atomic_text(path, "\n".join(lines) + "\n")


def _ensure_closed_table(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if any(line.startswith("## Closed") for line in text.splitlines()):
        return
    suffix = (
        "" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    )
    tp.atomic_text(
        path,
        text
        + suffix
        + "## Closed\n\n"
        + "| Item | Closed | Disposition |\n"
        + "|---|---|---|\n",
    )


def _clarification_message(
    decision: dict[str, Any], card: dict[str, Any]
) -> str:
    marked = decision.get("marked_choices") or []
    has_writing = bool(decision.get("write_in"))
    if len(marked) > 1:
        observed = "marks in " + " and ".join(str(value).title() for value in marked)
    elif marked and has_writing:
        observed = f"a mark in {str(marked[0]).title()} and handwriting on the write-in line"
    elif has_writing:
        observed = "handwriting on the write-in line that I could not safely interpret"
    else:
        observed = "a pen mark that I could not safely match to one answer"
    return (
        f"I need to clarify one answer on {card.get('headline', 'your Decisions document')}. "
        f"Question: {decision['label']} I saw {observed}. Please reply with the answer you intended."
    )


def _default_notifier(message: str, crop_path: Path | None) -> None:
    del crop_path  # Reserved for outbound picture support in mc-am50p.
    completed = subprocess.run(
        [str(IMESSAGE_ALERT), message], capture_output=True, text=True, timeout=240
    )
    if completed.returncode != 0:
        raise DecisionConsumerV3Error(
            completed.stderr.strip() or completed.stdout.strip() or "iMessage send failed"
        )


def _card_headlines(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(card["item_id"]): str(card.get("headline", ""))
        for card in manifest.get("cards", [])
        if isinstance(card, dict) and card.get("item_id")
    }


def _answered_entries_by_item(
    resolution: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Group this document's ANSWERED decisions by the row (item_id) they close.

    Only "answered" is a due-a-receipt status (mc-9sy2v / pen_chain_probe.py's
    own rule at check_document): deferred, discarded, unanswered and
    needs_review record nothing here and never had a receipt requirement.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for decision in resolution.get("decisions", []):
        if not isinstance(decision, dict) or decision.get("status") != "answered":
            continue
        item_id = str(decision.get("item_id", ""))
        if not item_id:
            continue
        grouped.setdefault(item_id, []).append(decision)
    return grouped


def _build_receipt(
    *,
    page_id: str,
    document_date: str,
    item_id: str,
    title: str,
    answered: list[dict[str, Any]],
    queue_sha: str,
    resolution_path: str,
    resolution_source: str,
) -> dict[str, Any]:
    """One row's answered questions, in consume_decisions.build_receipt's shape.

    Every reader of a receipt (pen_chain_probe.py, status-dashboard's
    reconcile.py) understands ONE schema, the one build_receipt writes for
    schema-1 rows. A v3 row's receipt reuses the same keys rather than
    inventing a second one; only the per-question detail behind "answers"
    differs because v3 answers are not always a bare YES/NO.
    """
    ordered = sorted(answered, key=lambda decision: decision.get("decision_number", 0))
    chosen = "; ".join(
        f"{decision.get('choice')} \"{decision.get('label')}\"" for decision in ordered
    )
    return {
        "tool": "consume_decisions",
        "schema_version": cd.RECEIPT_SCHEMA_VERSION,
        "page_id": page_id,
        "document_date": document_date,
        "item_id": item_id,
        "row_title": title,
        "recorded_at": dt.datetime.now().astimezone().isoformat(),
        "disposition": (
            f"PEN {document_date}: {chosen}. Recorded from the {document_date} "
            f"decisions document by decision_consumer_v3 on "
            f"{dt.date.today().isoformat()}."
        ),
        "answers": [
            {
                "entry_id": decision.get("entry_id"),
                "card_id": decision.get("card_id"),
                "decision_number": decision.get("decision_number"),
                "label": decision.get("label"),
                "choice": decision.get("choice"),
                "stroke_ids": decision.get("stroke_ids", []),
            }
            for decision in ordered
        ],
        "resolution_path": resolution_path,
        "resolution_source": resolution_source,
        "review_queue_sha256": queue_sha,
    }


def _same_recorded_answer(existing: dict[str, Any], receipt: dict[str, Any]) -> bool:
    original_queue_sha = existing.get("review_queue_sha256")
    original_disposition = existing.get("disposition")
    if not isinstance(original_queue_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", original_queue_sha):
        return False
    if not isinstance(original_disposition, str):
        return False
    disposition, separator, recorded_date = original_disposition.rpartition(" on ")
    if not separator or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.", recorded_date):
        return False
    if disposition != receipt["disposition"].rpartition(" on ")[0]:
        return False
    return cd._same_receipt(
        existing, dict(receipt, review_queue_sha256=original_queue_sha, disposition=original_disposition)
    )


def _write_receipt_if_needed(
    receipts_dir: Path, page_id: str, item_id: str, receipt: dict[str, Any], *,
    already_recorded: bool,
) -> None:
    """Write one receipt atomically, without ever duplicating or corrupting it.

    tp.atomic_json stages the write beside the destination (mkstemp in the
    same directory) then renames it into place, the same staged-then-rename
    shape consume_decisions.stage_receipts/publish_receipts use for schema-1
    receipts, so a crash mid-write never leaves a torn file.

    Idempotency is proved before writing, not assumed: an identical receipt
    already on disk (recorded_at aside, per consume_decisions._same_receipt)
    is left alone so a rerun is a no-op; a DIFFERENT one already at this path
    is refused rather than silently overwritten, the same rule
    consume_decisions._refuse_forged_overwrite enforces for schema-1 receipts.
    """
    path = cd.receipt_path(receipts_dir, page_id, item_id)
    existing: dict[str, Any] | None = None
    if path.is_file():
        try:
            candidate = cd._read_json(path)
        except (OSError, ValueError):
            candidate = None
        if isinstance(candidate, dict):
            existing = candidate
    if existing is not None:
        if cd._same_receipt(existing, receipt) or (
            already_recorded and _same_recorded_answer(existing, receipt)
        ):
            return
        raise DecisionConsumerV3Error(
            f"a receipt already exists at {path} for {page_id}/{item_id} and "
            "records something different; refusing to overwrite it"
        )
    tp.atomic_json(path, receipt)


def consume(
    manifest: dict[str, Any],
    resolution: dict[str, Any],
    *,
    queue_path: Path,
    state_path: Path,
    document_date: str,
    apply: bool = False,
    notifier: Callable[[str, Path | None], None] | None = None,
    notifications_enabled: bool = True,
    receipts_dir: Path | None = None,
    resolution_path: str = "",
    resolution_source: str = "",
) -> ConsumeResult:
    """Plan or apply one v3 document. Dry run has no external side effects."""
    queue_path = Path(queue_path)
    state_path = Path(state_path)
    notifier = notifier or _default_notifier
    # mc-z5hy3: the v3 path had no note surface at all, so four handwritten
    # notes on 2026-09-03 reached nobody. Answers are still recorded; the
    # notes ride along in the report so a human knows to open the crops.
    notes = [
        note
        for note in resolution.get("note_regions") or []
        if isinstance(note, dict)
    ]
    answered_by_item = _answered_entries_by_item(resolution)
    answered_item_ids = sorted(answered_by_item)

    def plan(queue_copy: Path, current_state: dict[str, Any]):
        rows = active_rows(queue_copy)
        _validate_document(manifest, resolution, rows, current_state)
        planned_state, clarification_ids = qs.plan_resolution(
            current_state, resolution, resolved_on=document_date
        )
        changed_ids = sorted(
            entry_id
            for entry_id, record in planned_state["entries"].items()
            if current_state["entries"].get(entry_id) != record
        )
        entries_by_item: dict[str, list[dict[str, Any]]] = {}
        for entry in manifest["entries"]:
            entries_by_item.setdefault(entry["item_id"], []).append(entry)
        closed: list[str] = []
        kept: list[str] = []
        def row_is_terminal(row: ActiveRow) -> bool:
            return all(
                planned_state["entries"].get(
                    question_entry_id(row.item_id, key), {}
                ).get("state")
                in qs.TERMINAL_STATES
                for key in row.question_keys
            )

        if any(
            row_is_terminal(rows[item_id])
            for item_id in entries_by_item
            if item_id in rows
        ):
            _ensure_closed_table(queue_copy)
        for item_id, entries in entries_by_item.items():
            if item_id not in rows:
                continue
            if row_is_terminal(rows[item_id]):
                row = rows[item_id]
                tp.close_review_queue_row(queue_copy, item_id)
                _rewrite_closed_disposition(
                    queue_copy,
                    row,
                    f"PEN {document_date}: every question was answered or discarded. Per-question details are in question-state.json.",
                )
                closed.append(item_id)
            else:
                kept.append(item_id)
        return rows, planned_state, clarification_ids, changed_ids, closed, kept

    with tempfile.TemporaryDirectory(prefix="consume-decisions-v3-") as temporary:
        queue_copy = Path(temporary) / "REVIEW-QUEUE.md"
        queue_copy.write_bytes(queue_path.read_bytes())
        current_state = qs.load_state(state_path)
        rows, planned_state, clarification_ids, changed_ids, closed, kept = plan(
            queue_copy, current_state
        )
        if not apply:
            return ConsumeResult(
                manifest["page_id"], changed_ids, closed, kept,
                clarification_ids, [], [], False, notes,
                answered_item_ids=answered_item_ids,
            )

        # mc-9sy2v: an answered decision must leave a receipt behind. Fail
        # closed, before touching the queue or the state file, rather than
        # apply the answer and discover afterwards there is nowhere to record
        # it happened.
        if answered_by_item and receipts_dir is None:
            raise DecisionConsumerV3Error(
                "an answered decision requires receipts_dir to record a receipt"
            )

        with tp.queue_lock(queue_path):
            with qs.locked_state(state_path) as locked_current:
                queue_copy.write_bytes(queue_path.read_bytes())
                rows, planned_state, clarification_ids, changed_ids, closed, kept = plan(
                    queue_copy, locked_current
                )
                if planned_state != locked_current or not state_path.exists():
                    qs.write_state(state_path, planned_state)
                new_queue_text = queue_copy.read_text(encoding="utf-8")
                queue_sha_after = tp.sha256_bytes(new_queue_text.encode("utf-8"))
                expected_sha = tp.sha256_file(queue_path)
                if queue_sha_after != expected_sha:
                    tp.replace_queue_verified(queue_path, new_queue_text, expected_sha)

                # The row's receipt goes down right after the queue and state
                # commit it corresponds to, still inside both locks: any two
                # runs racing to consume the same page serialize on those
                # locks already, so no other writer can be touching this
                # page's receipt files concurrently.
                headlines = _card_headlines(manifest)
                for item_id, answered in answered_by_item.items():
                    title = (
                        rows[item_id].title
                        if item_id in rows
                        else headlines.get(item_id, item_id)
                    )
                    receipt = _build_receipt(
                        page_id=manifest["page_id"],
                        document_date=document_date,
                        item_id=item_id,
                        title=title,
                        answered=answered,
                        queue_sha=queue_sha_after,
                        resolution_path=resolution_path,
                        resolution_source=resolution_source,
                    )
                    already_recorded = all(
                        locked_current["entries"].get(decision["entry_id"], {}).get("source")
                        == manifest["page_id"]
                        and decision["entry_id"] not in changed_ids
                        for decision in answered
                    )
                    _write_receipt_if_needed(
                        receipts_dir, manifest["page_id"], item_id, receipt,
                        already_recorded=already_recorded,
                    )

        decisions = {item["entry_id"]: item for item in resolution["decisions"]}
        cards = {item["card_id"]: item for item in manifest["cards"]}
        sent: list[str] = []
        failed: list[str] = []
        for entry_id in clarification_ids if notifications_enabled else []:
            decision = decisions[entry_id]
            crop = decision.get("write_in", {}).get("path") if decision.get("write_in") else None
            try:
                notifier(
                    _clarification_message(decision, cards[decision["card_id"]]),
                    Path(crop) if crop else None,
                )
            except Exception:
                failed.append(entry_id)
            else:
                sent.append(entry_id)
        if sent:
            with qs.locked_state(state_path) as latest:
                qs.write_state(state_path, qs.mark_notified(latest, sent))
        return ConsumeResult(
            manifest["page_id"], changed_ids, closed, kept,
            clarification_ids, sent, failed, True, notes,
            answered_item_ids=answered_item_ids,
        )


def render_report(result: ConsumeResult) -> str:
    mode = "APPLY" if result.applied else "DRY RUN"
    lines = [f"consume_decisions v3 ({mode})", f"page {result.page_id}"]
    lines.append(
        f"question states changed: {len(result.changed_entry_ids)}; "
        f"rows closed: {len(result.closed_item_ids)}; rows kept Active: {len(result.kept_item_ids)}"
    )
    if result.clarification_entry_ids:
        lines.append(
            f"needs clarification: {len(result.clarification_entry_ids)}"
        )
    if result.notes:
        lines.append(
            f"UNREAD: {len(result.notes)} handwritten note(s) nobody has read. "
            "This tool cannot read them; open the images below."
        )
        for note in result.notes:
            lines.append(
                f"  NOTE page {note.get('page', '?')} "
                f"({len(note.get('stroke_ids') or [])} strokes) -> "
                f"{note.get('path', 'no crop rendered')}"
            )
    if result.notifications_failed:
        lines.append(
            f"iMessage delivery unconfirmed: {len(result.notifications_failed)}"
        )
    if result.answered_item_ids:
        verb = "written" if result.applied else "due (would be written on apply)"
        lines.append(
            f"receipts {verb} for {len(result.answered_item_ids)} row(s): "
            + ", ".join(
                f"{result.page_id}.{item_id}" for item_id in result.answered_item_ids
            )
        )
    return "\n".join(lines)
