#!/usr/bin/env python3
"""Consume resolved pen decisions into recorded dispositions on the review queue.

`today_page.resolve()` splits ink into `actions` (executable) and `decisions`
(recorded only), and `today_page.execute()` iterates only `actions`. Every
entry the decisions document produces has kind "decision", so nothing ever
consumed the reader's pen answers: the 2026-08-16 document resolved 30 correct
answers that were never acted on, and the 2026-08-19 document re-asked 29 of
the same questions. This module is the missing hop.

It replays the ink through the CURRENT resolver, groups the answers by the
queue row they belong to (`item_id`), appends a durable "Pen dispositions"
section to REVIEW-QUEUE.md, and moves each answered row out of Active with
`today_page.close_review_queue_row`.

Safety, in order of application:

1. The resolution is VALIDATED before anything is planned. Only YES and NO are
   accepted, every answer must correspond exactly to a manifest entry, and a
   duplicated or self-conflicting entry fails the whole document closed.
2. An unresolved mark that cannot be attributed to a row BLOCKS the whole
   document. A mark nobody can explain is never assumed harmless.
3. A row is HELD when it is no longer in Active, when an unresolved mark
   belongs to it, or when its receipt on disk is unreadable.
4. The queue write is a locked compare-and-swap: the live file is re-read
   immediately before writing and the write is abandoned if it changed.
5. Receipts are staged and validated first, then the queue is written, then the
   receipts are published, with a journal that makes a crash at any boundary
   recoverable deterministically.

Nothing is written unless `--apply` is given. A dry run writes NOTHING outside
its own temporary directory: no queue, no receipts, no journal, no lock file,
and nothing at all inside an archive folder. Even on `--apply`, an artifact
already in an archive folder is never overwritten, because those files are the
record of what the reader's device produced on the day, including the renderings of
his handwriting. This module never renders images; it only reports the paths of
renderings that already exist.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import today_page as tp  # noqa: E402  (path shim above is deliberate)

REPO_ROOT = tp.REPO_ROOT
TODAY_PAGES = tp.ANSWER_RECORD_ROOT
RECEIPTS_DIR = TODAY_PAGES / "receipts"
REVIEW_QUEUE = tp._CONFIG.queue
# Operator alerts go through the configured alert hook (a no-op when unset).
BUS_ALERT = Path(__file__).resolve().parent / "hooks" / "alert.sh"
QUESTION_STATE = tp.QUESTION_STATE_PATH

EXIT_OK = 0
EXIT_HOLD = 1
EXIT_ERROR = 2

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# The pen offers exactly two boxes. Anything else in a resolution file is a
# corrupt or hand-edited artifact, not an answer the reader gave.
VALID_CHOICES = frozenset({"YES", "NO"})

RECEIPT_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1
JOURNAL_STATES = ("prepared", "queue_written", "published")
LOCK_TIMEOUT_SECONDS = 30.0

HOLD_REASONS = {
    "row_not_in_active": (
        "the queue row is no longer in the Active table (the row moved or its "
        "wording changed, so its id no longer matches)"
    ),
    "entry_answered_both_ways": "one option carries both a YES and a NO mark",
    "ambiguity_touches_row": (
        "the document has an unresolved mark whose candidates belong to this row"
    ),
    "document_blocked": (
        "the document has an unresolved mark that cannot be attributed to any "
        "row, so nothing on this document is recorded"
    ),
    "receipt_unreadable": (
        "a receipt already exists at this row's receipt path but does not "
        "describe this row, so the record on disk cannot be trusted"
    ),
}


class ConsumeError(RuntimeError):
    """Anything that makes a run unsafe to continue."""


class ValidationError(ConsumeError):
    """The input artifact is not trustworthy. Nothing is planned from it."""


class UnsupportedDocumentError(ValidationError):
    """This tool does not understand the document's layout. Skip it, do not fail.

    An old page (2026-07-28 is decisions-v2.1) is not evidence of anything
    wrong with today's documents, so it must not take the whole run down with
    it. It is named in the report and skipped.
    """


class QueueChangedError(ConsumeError):
    """The review queue changed under us between the read and the write."""


class NoteRecord:
    """Handwriting on a card. Never a defect, always something to read."""

    def __init__(
        self,
        card_id: str,
        headline: str,
        source: str,
        stroke_ids: list[str],
        png: Path | None,
        answered: bool = False,
    ) -> None:
        self.card_id = card_id
        self.headline = headline
        self.source = source  # note_region | card_body
        self.stroke_ids = stroke_ids
        self.png = png
        self.answered = answered

    @property
    def png_exists(self) -> bool:
        return bool(self.png and self.png.is_file())

    def describe(self) -> str:
        where = (
            "in the note lines"
            if self.source == "note_region"
            else "written across the card"
        )
        image = (
            str(self.png)
            if self.png_exists
            else f"{self.png} (not rendered yet)"
            if self.png
            else "no image rendered"
        )
        answered = " (this card was also answered)" if self.answered else ""
        return (
            f"{self.headline}{answered}: {len(self.stroke_ids)} strokes {where} "
            f"-> {image}"
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def document_date(processed_dir: Path, page_id: str) -> str:
    """Date of the document, from the archive folder name or the page id."""
    match = DATE_RE.search(processed_dir.parent.name)
    if match:
        return match.group(1)
    match = DATE_RE.search(page_id)
    if match:
        return match.group(1)
    raise ConsumeError(f"Cannot determine a document date for {processed_dir}")


def find_manifest(processed_dir: Path, page_id: str) -> dict[str, Any] | None:
    """The manifest for this exact page, or None when it cannot be identified."""
    for candidate in sorted(processed_dir.parent.glob("*.manifest.json")):
        try:
            manifest = _read_json(candidate)
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("page_id") == page_id:
            return manifest
    return None


def _answer_set(resolution: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(entry.get("entry_id")), str(entry.get("choice")))
        for entry in resolution.get("decisions", [])
    }


class Document:
    """One processed reMarkable decisions document."""

    def __init__(self, processed_dir: Path) -> None:
        processed_dir = Path(processed_dir).resolve()
        self.processed_dir = processed_dir
        resolution_path = processed_dir / "resolution.json"
        if not resolution_path.is_file():
            raise ConsumeError(f"No resolution.json in {processed_dir}")
        self.resolution_path = resolution_path
        self.stored_resolution = _read_json(resolution_path)
        self.page_id = str(self.stored_resolution.get("page_id", ""))
        if not self.page_id:
            raise ConsumeError(f"{resolution_path} has no page_id")
        self.date = document_date(processed_dir, self.page_id)
        self.manifest = find_manifest(processed_dir, self.page_id)

        # The resolution.json files on disk were written by whichever resolver
        # was current on the day. Several predate the mc-rrjv adjacent-box fix
        # and carry ambiguities the current resolver does not produce, which
        # would hold answers that are not actually ambiguous. Replay the ink
        # through today_page.resolve() as it stands now, and fall back to the
        # stored file only when the ink or the manifest is unavailable.
        self.resolution = self.stored_resolution
        self.resolution_source = "stored"
        self.resolution_note = ""
        self.reresolved: dict[str, Any] | None = None
        # The replayed ink, kept by stroke id: a mark's bounding box is a
        # rectangle around a curve, so the box corners can sit somewhere the
        # pen never went. Where the real polyline is on disk, geometry uses it.
        self.strokes_by_id: dict[str, dict[str, Any]] = {}
        self._reresolve()

        self.resolution_changed = _answer_set(self.resolution) != _answer_set(
            self.stored_resolution
        )
        self.ambiguity_delta = len(
            self.stored_resolution.get("ambiguities", [])
        ) - len(self.resolution.get("ambiguities", []))
        self.decisions: list[dict[str, Any]] = list(
            self.resolution.get("decisions", [])
        )
        self.ambiguities: list[dict[str, Any]] = list(
            self.resolution.get("ambiguities", [])
        )

        self.entry_to_item: dict[str, str] = {}
        self.entries_by_id: dict[str, dict[str, Any]] = {}
        self.item_headlines: dict[str, str] = {}
        for decision in self.decisions:
            entry_id = str(decision.get("entry_id", ""))
            item_id = str(decision.get("item_id", ""))
            if entry_id and item_id:
                self.entry_to_item[entry_id] = item_id
        if self.manifest:
            for entry in self.manifest.get("entries", []):
                if entry.get("kind") != "decision":
                    continue
                entry_id, item_id = entry.get("entry_id"), entry.get("item_id")
                if entry_id and item_id:
                    self.entry_to_item[str(entry_id)] = str(item_id)
                    self.entries_by_id[str(entry_id)] = entry
            for card in self.manifest.get("cards", []):
                item_id, headline = card.get("item_id"), card.get("headline")
                if item_id and headline:
                    self.item_headlines[str(item_id)] = str(headline)

    def _reresolve(self) -> None:
        """Replay strokes.json through the CURRENT resolver, or record why not."""
        strokes_path = self.processed_dir / "strokes.json"
        if self.manifest is None:
            self.resolution_note = (
                "no manifest on disk matches this page, so the ink could not be "
                "replayed; the stored resolution is used as recorded"
            )
            return
        if not strokes_path.is_file():
            self.resolution_note = (
                "no strokes.json on disk, so the ink could not be replayed; the "
                "stored resolution is used as recorded"
            )
            return
        try:
            ink = _read_json(strokes_path)
            fresh = tp.resolve(self.manifest, ink)
        except Exception as exc:
            self.resolution_note = (
                f"the ink could not be replayed ({type(exc).__name__}: {exc}); "
                "the stored resolution is used as recorded"
            )
            return
        self.resolution = fresh
        self.reresolved = fresh
        self.resolution_source = "re-resolved"
        for stroke in ink.get("strokes", []) if isinstance(ink, dict) else []:
            stroke_id = stroke.get("stroke_id")
            if stroke_id:
                self.strokes_by_id[str(stroke_id)] = stroke

    def reresolved_path(self, stamp: str | None = None) -> Path:
        """The next free name. An archived artifact is NEVER overwritten.

        Everything already in an archive folder is the record of what the reader's
        device produced that day: his ink, and the renderings of his
        handwriting. Those are evidence. A regenerated artifact goes beside
        them under a fresh name, so a second run on the same day cannot stand
        on top of the first.
        """
        stamp = stamp or dt.date.today().isoformat()
        candidate = self.processed_dir / f"resolution-reresolved-{stamp}.json"
        counter = 2
        while candidate.exists():
            candidate = (
                self.processed_dir
                / f"resolution-reresolved-{stamp}-{counter}.json"
            )
            counter += 1
        return candidate

    def write_reresolved(self, stamp: str | None = None) -> Path | None:
        """Save the replayed resolution BESIDE the original, never over it."""
        if self.reresolved is None:
            return None
        path = self.reresolved_path(stamp)
        if path.exists():  # belt and braces: reresolved_path already avoids this
            raise ConsumeError(
                f"{path} already exists; an archived artifact is never "
                "overwritten"
            )
        payload = dict(self.reresolved)
        payload["reresolved_from"] = str(self.resolution_path)
        payload["reresolved_at"] = dt.datetime.now().astimezone().isoformat()
        payload["reresolved_by"] = "consume_decisions"
        tp.atomic_json(path, payload)
        return path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Document {self.date} {self.page_id}>"

    # -- validation ---------------------------------------------------------

    def validate(self) -> None:
        """Fail the whole document closed if its content cannot be trusted.

        Runs BEFORE any planning. An artifact that has been hand-edited,
        truncated, or written by a tool we do not recognise must never reach
        the code that closes rows in the reader's queue.
        """
        resolution = self.resolution
        if not isinstance(resolution, dict):
            raise ValidationError("the resolution is not an object")
        if resolution.get("page_id") != self.page_id:
            raise ValidationError(
                "the resolution's page id does not match the document"
            )
        # This class is the legacy schema-1 consumer. Schema 2 is dispatched
        # to decision_consumer_v3 before Document.validate() is called. Do not
        # compare with today_page.SCHEMA_VERSION here: generation advancing to
        # schema 2 must not make archived schema-1 ink unreadable.
        if resolution.get("schema_version") != 1:
            raise UnsupportedDocumentError(
                f"the resolution's schema_version is "
                f"{resolution.get('schema_version')!r}, this tool "
                "understands legacy schema 1"
            )
        # layout_version is checked against an allowlist, not the single
        # current LAYOUT_VERSION: the appendix-markdown change (mc-vjz5) bumped
        # LAYOUT_VERSION without touching answer-box geometry or ink
        # resolution, and every archived document not yet re-answered would
        # otherwise be refused outright. A version outside the allowlist is
        # still refused -- a document written by a tool we do not recognise,
        # or one whose layout changed in a way that could move an answer box,
        # must never reach the code that closes rows in the reader's queue.
        layout_version = resolution.get("layout_version")
        if layout_version not in tp.ANSWER_COMPATIBLE_LAYOUT_VERSIONS:
            raise UnsupportedDocumentError(
                f"the resolution's layout_version is {layout_version!r}, "
                f"this tool understands {sorted(tp.ANSWER_COMPATIBLE_LAYOUT_VERSIONS)!r}"
            )
        decisions = resolution.get("decisions")
        if not isinstance(decisions, list):
            raise ValidationError("the resolution's decisions are not a list")
        if not isinstance(resolution.get("ambiguities", []), list):
            raise ValidationError("the resolution's ambiguities are not a list")

        seen: dict[str, str] = {}
        for index, decision in enumerate(decisions):
            where = f"decision {index + 1}"
            if not isinstance(decision, dict):
                raise ValidationError(f"{where} is not an object")
            entry_id = decision.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValidationError(f"{where} has no entry id")
            choice = decision.get("choice")
            if choice not in VALID_CHOICES:
                raise ValidationError(
                    f"{where} ({entry_id}) has choice {choice!r}; only "
                    f"{sorted(VALID_CHOICES)} are answers a pen can give"
                )
            for field in ("item_id", "card_id", "label"):
                value = decision.get(field)
                if not isinstance(value, str) or not value:
                    raise ValidationError(f"{where} ({entry_id}) has no {field}")
            number = decision.get("decision_number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValidationError(
                    f"{where} ({entry_id}) has an invalid decision number"
                )
            if entry_id in seen:
                raise ValidationError(
                    f"{entry_id} appears more than once "
                    f"({seen[entry_id]} then {choice}); one option cannot carry "
                    "two marks, so nothing on this document is recorded"
                )
            seen[entry_id] = str(choice)

            if self.manifest is None:
                continue
            entry = self.entries_by_id.get(entry_id)
            if entry is None:
                raise ValidationError(
                    f"{where} names {entry_id}, which is not a decision entry "
                    "in this page's manifest"
                )
            for field in ("item_id", "card_id", "label", "decision_number"):
                if decision.get(field) != entry.get(field):
                    raise ValidationError(
                        f"{where} ({entry_id}) disagrees with the manifest on "
                        f"{field}: {decision.get(field)!r} against "
                        f"{entry.get(field)!r}"
                    )

    # -- ambiguity ----------------------------------------------------------

    # -- geometry -----------------------------------------------------------

    def _card_rect(self, card: dict[str, Any]) -> list[float]:
        """The whole printed card: x, bottom, x + width, top."""
        layout = card.get("layout", {})
        return [
            float(layout.get("x", 0.0)),
            float(layout.get("bottom", 0.0)),
            float(layout.get("x", 0.0)) + float(layout.get("width", 0.0)),
            float(layout.get("top", 0.0)),
        ]

    def _answer_boxes_on(self, page: int) -> list[list[float]]:
        if not self.manifest:
            return []
        return [
            entry_box["bbox"]
            for entry in self.manifest.get("entries", [])
            if entry.get("page") == page
            for entry_box in entry.get("boxes", [])
        ]

    def _answer_corridors_on(self, page: int) -> list[list[float]]:
        """One rectangle per card: everything its answer boxes span.

        The boxes of a card sit in a column of YES/NO pairs. The space BETWEEN
        them - between the two columns, and between the rows - is not prose
        space: a mark there is an answer nobody can read. The corridor is taken
        per card rather than per page so it stays tight around the answer
        column instead of swallowing the printed body.
        """
        if not self.manifest:
            return []
        by_card: dict[str, list[list[float]]] = {}
        for entry in self.manifest.get("entries", []):
            if entry.get("page") != page:
                continue
            boxes = [box["bbox"] for box in entry.get("boxes", [])]
            if boxes:
                by_card.setdefault(str(entry.get("card_id", "")), []).extend(boxes)
        corridors = []
        for boxes in by_card.values():
            corridors.append(
                [
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    max(box[2] for box in boxes),
                    max(box[3] for box in boxes),
                ]
            )
        return corridors

    def _card_at(self, page: int, x: float, y: float) -> dict[str, Any] | None:
        if not self.manifest:
            return None
        for card in self.manifest.get("cards", []):
            if card.get("page") != page:
                continue
            if tp._inside(x, y, self._card_rect(card)):
                return card
        return None

    def classify_mark(self, ambiguity: dict[str, Any]) -> dict[str, Any] | None:
        """The card this unattributable mark is written on, or None.

        The reader writes on these pages. A long stroke across a card's printed body
        is prose, not a defect, and blocking a document on it withholds the
        answers he did give. So a mark that lies wholly clear of the answer
        column is handwriting.

        Anything that TOUCHES the answer geometry is not, whatever its centre
        says. Testing the centre alone let a mark long enough to cover a YES
        box read as prose because its middle happened to fall in the card body,
        which is a misread answer presented as a note. Touching an answer box,
        or the corridor its card's boxes span, fails the document closed.
        """
        bbox = ambiguity.get("bbox")
        page = ambiguity.get("page")
        if not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(page, int):
            return None  # no geometry to reason from, so fail closed
        try:
            mark = [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
        except (TypeError, ValueError):
            return None
        mark = [
            min(mark[0], mark[2]),
            min(mark[1], mark[3]),
            max(mark[0], mark[2]),
            max(mark[1], mark[3]),
        ]
        targets = [tp._expanded(box) for box in self._answer_boxes_on(page)]
        targets += self._answer_corridors_on(page)
        ink = self._mark_polyline(ambiguity, page)
        for target in targets:
            if _polyline_hits(ink, target) if ink else tp._intersects(mark, target):
                return None
        cx, cy = (mark[0] + mark[2]) / 2, (mark[1] + mark[3]) / 2
        return self._card_at(page, cx, cy)

    def _mark_polyline(
        self, ambiguity: dict[str, Any], page: int
    ) -> list[tuple[float, float]]:
        """The mark's real points, when the ink for it is on disk.

        Falling back to the bounding box is the safe direction (it can only
        over-block), but it over-blocks real handwriting: the 2026-08-16 page-8
        note sweeps from x=65 to x=495 and its bbox clips the YES column, while
        the ink itself is 140 points above the nearest box. Reading it as an
        unreadable answer would withhold that page's answers and file the reader's
        note as a fault.
        """
        stroke = self.strokes_by_id.get(str(ambiguity.get("stroke_id", "")))
        if not stroke or stroke.get("page") != page:
            return []
        points: list[tuple[float, float]] = []
        for point in stroke.get("points", []):
            try:
                points.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError, IndexError):
                return []  # unreadable ink, so fall back to the bounding box
        return points

    def ambiguous_items(
        self,
    ) -> tuple[set[str], list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
        """Rows touched by unresolved marks, marks that block, marks that are notes.

        A mark in the blocking list lands in or between ANSWER boxes and cannot
        be charged to a row: an unreadable answer, which fails the document
        closed. A mark in the notes list is handwriting on a card and never
        blocks anything.
        """
        touched: set[str] = set()
        blocking: list[dict[str, Any]] = []
        notes: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for ambiguity in self.ambiguities:
            entry_ids = [
                str(value)
                for value in ambiguity.get("candidate_entry_ids", [])
                if value
            ]
            single = ambiguity.get("entry_id")
            if single:
                entry_ids.append(str(single))
            if entry_ids:
                unresolved = [
                    entry_id
                    for entry_id in entry_ids
                    if entry_id not in self.entry_to_item
                ]
                if unresolved:
                    # A named candidate that resolves to no row means manifest
                    # and resolution disagree about what is on the page. Two
                    # candidates that both belong to ONE row is normal (a mark
                    # between two options of the same card) and holds that row.
                    blocking.append(ambiguity)
                    continue
                touched |= {self.entry_to_item[entry_id] for entry_id in entry_ids}
                continue
            card = self.classify_mark(ambiguity)
            if card is None:
                blocking.append(ambiguity)
            else:
                notes.append((ambiguity, card))
        return touched, blocking, notes

    # -- notes --------------------------------------------------------------

    def note_png(self, card_id: str) -> Path:
        """The newest rendering of this card's handwriting.

        A recut (notes-recut-<date>/) supersedes the original crop, which is
        kept because an archived render is never overwritten. Point a reader at
        the complete one.
        """
        recuts = sorted(
            (
                path
                for path in self.processed_dir.glob("notes-recut-*")
                if path.is_dir() and (path / f"{card_id}.png").is_file()
            ),
            reverse=True,
        )
        if recuts:
            return recuts[0] / f"{card_id}.png"
        return self.processed_dir / "notes" / f"{card_id}.png"

    def notes(self, answered_items: set[str] | None = None) -> list[NoteRecord]:
        """Every card carrying handwriting, from both sources, in card order."""
        answered_items = answered_items or set()
        cards = (
            {str(card["card_id"]): card for card in self.manifest.get("cards", [])}
            if self.manifest
            else {}
        )
        collected: "OrderedDict[str, NoteRecord]" = OrderedDict()

        def add(card_id: str, source: str, stroke_ids: list[str], label: str) -> None:
            card = cards.get(card_id, {})
            headline = str(card.get("headline") or label or card_id)
            item_id = str(card.get("item_id", ""))
            existing = collected.get(card_id)
            if existing is not None:
                existing.stroke_ids = sorted(set(existing.stroke_ids) | set(stroke_ids))
                return
            collected[card_id] = NoteRecord(
                card_id=card_id,
                headline=headline,
                source=source,
                stroke_ids=sorted(stroke_ids),
                png=self.note_png(card_id),
                answered=item_id in answered_items,
            )

        for region in self.resolution.get("note_regions", []):
            add(
                str(region.get("card_id", "")),
                "note_region",
                [str(s) for s in region.get("stroke_ids", [])],
                str(region.get("label", "")),
            )
        _, _, mark_notes = self.ambiguous_items()
        for ambiguity, card in mark_notes:
            add(
                str(card.get("card_id", "")),
                "card_body",
                [str(ambiguity.get("stroke_id", ""))],
                str(card.get("headline", "")),
            )
        return list(collected.values())


def describe_ambiguity(ambiguity: dict[str, Any]) -> str:
    parts = [str(ambiguity.get("reason", "unknown"))]
    stroke = ambiguity.get("stroke_id")
    if stroke:
        parts.append(f"stroke {stroke}")
    page = ambiguity.get("page")
    if page:
        parts.append(f"page {page}")
    candidates = ambiguity.get("candidate_entry_ids") or (
        [ambiguity["entry_id"]] if ambiguity.get("entry_id") else []
    )
    parts.append(
        f"candidates {', '.join(str(c) for c in candidates)}"
        if candidates
        else "no candidate entries named"
    )
    return " / ".join(parts)


def discover_processed_dirs(root: Path = TODAY_PAGES) -> list[Path]:
    """Every archived processed directory that has a resolution, in date order."""
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not DATE_RE.fullmatch(child.name):
            continue
        processed = child / "processed"
        if (processed / "resolution.json").is_file():
            found.append(processed)
    return found


# ---------------------------------------------------------------------------
# The review queue: raw rows, locking, compare-and-swap
# ---------------------------------------------------------------------------


def raw_ask_of(line: str) -> str:
    """The Ask cell exactly as written, pipes and spacing intact.

    Splitting into cells and rejoining with a pipe is lossy twice over: the
    cell split strips the spaces around each pipe, and reading the Ask back out
    of the written Closed row truncates it at the first pipe because that table
    has three columns. Slicing the raw line after the third delimiter keeps the
    original bytes.
    """
    body = line.strip("|")
    parts = body.split("|", 3)
    return parts[3].strip() if len(parts) > 3 else ""


def active_raw_rows(path: Path) -> dict[str, str]:
    """item_id -> the row's Ask, exactly as it sits in the file.

    Walks the same rows with the same authority as parse_review_queue
    (tp._split_row decides what is a row) but keeps the Ask untouched.
    """
    rows: dict[str, str] = {}
    active = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            active = line.startswith("## Active")
            continue
        if not active:
            continue
        cells = tp._split_row(line)
        if cells is None or len(cells) < 4:
            continue
        import hashlib

        stable = hashlib.sha256(
            (
                f"{tp.clean_markdown(cells[0])}\0{tp.clean_markdown(cells[1])}\0"
                f"{tp.clean_markdown(cells[2])}"
            ).encode()
        ).hexdigest()[:12]
        rows[stable] = raw_ask_of(line)
    return rows


@contextmanager
def queue_lock(queue_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[Path]:
    """The review queue's lock. One lock, shared with every other writer.

    This delegates to today_page.queue_lock rather than opening a lock of its
    own: a lock only one writer takes is not a lock, and today_page holds the
    other writers (execute() and the 06:00 generator both close rows through
    close_review_queue_row). Re-entering it on the same thread is a no-op, so
    a run can hold it across a whole document while the functions underneath
    take it again for their own writes.
    """
    try:
        with tp.queue_lock(queue_path, timeout=timeout) as lock_path:
            yield lock_path
    except tp.QueueMovedError:
        raise
    except tp.TodayPageError as exc:
        raise ConsumeError(str(exc)) from exc


def commit_queue(queue_path: Path, new_text: str, expected_sha: str) -> str:
    """Compare-and-swap: abandon the write if the live file moved underneath us.

    The comparison and the replacement are ONE step inside the shared lock
    (tp.replace_queue_verified), because a comparison that a separate write
    follows leaves a window a competing writer can land in, and that writer's
    work is what disappears. Returns the sha256 of what was written.
    """
    with queue_lock(queue_path):
        try:
            return tp.replace_queue_verified(queue_path, new_text, expected_sha)
        except tp.QueueMovedError as exc:
            raise QueueChangedError(
                "the review queue changed while this run was working "
                f"(expected {exc.expected[:12]}, found {exc.found[:12]}); "
                "no queue change was made"
            ) from exc


# ---------------------------------------------------------------------------
# Receipts: validated, staged, journalled, published
# ---------------------------------------------------------------------------


def receipt_path(receipts_dir: Path, page_id: str, item_id: str) -> Path:
    return receipts_dir / f"{page_id}.{item_id}.json"


def read_receipt(path: Path, page_id: str, item_id: str) -> dict[str, Any]:
    """Load one receipt and prove it describes this exact row.

    A file merely EXISTING at the receipt path is not proof of anything. A
    zero-byte file used to make a row silently skip, which is the worst of both
    outcomes: the answer is neither recorded nor reported.
    """
    try:
        data = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{path} is not readable JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{path} is not a receipt object")
    if data.get("tool") != "consume_decisions":
        raise ValidationError(f"{path} was not written by this tool")
    if data.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValidationError(
            f"{path} has receipt schema {data.get('schema_version')!r}, "
            f"expected {RECEIPT_SCHEMA_VERSION}"
        )
    if data.get("page_id") != page_id or data.get("item_id") != item_id:
        raise ValidationError(
            f"{path} describes {data.get('page_id')}/{data.get('item_id')}, "
            f"not {page_id}/{item_id}"
        )
    answers = data.get("answers")
    if not isinstance(answers, list) or not answers:
        raise ValidationError(f"{path} records no answers")
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("choice") not in VALID_CHOICES:
            raise ValidationError(f"{path} records an answer that is not YES or NO")
    return data


def build_receipt(
    document: Document, plan: "RowPlan", queue_sha: str
) -> dict[str, Any]:
    return {
        "tool": "consume_decisions",
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "page_id": document.page_id,
        "document_date": document.date,
        "item_id": plan.item_id,
        "row_title": plan.title,
        "recorded_at": dt.datetime.now().astimezone().isoformat(),
        "disposition": plan.disposition,
        "answers": [
            {
                "entry_id": answer.get("entry_id"),
                "card_id": answer.get("card_id"),
                "decision_number": answer.get("decision_number"),
                "label": answer.get("label"),
                "choice": answer.get("choice"),
                "stroke_ids": answer.get("stroke_ids", []),
            }
            for answer in plan.answers
        ],
        "resolution_path": str(document.resolution_path),
        "resolution_source": document.resolution_source,
        "review_queue_sha256": queue_sha,
    }


def journal_path(receipts_dir: Path, run_id: str) -> Path:
    return receipts_dir / f"journal-{run_id}.json"


def stage_receipts(
    receipts_dir: Path,
    run_id: str,
    document: Document,
    plans: list["RowPlan"],
    queue_sha: str,
) -> list[dict[str, str]]:
    """Write every receipt to a staging area and read each one back."""
    staging = receipts_dir / f"staging-{run_id}"
    staging.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, str]] = []
    for plan in plans:
        final = receipt_path(receipts_dir, document.page_id, plan.item_id)
        temporary = staging / final.name
        tp.atomic_json(temporary, build_receipt(document, plan, queue_sha))
        # Prove the staged file is a valid receipt for this row before the
        # queue is touched, so a bad receipt can never follow a queue change.
        read_receipt(temporary, document.page_id, plan.item_id)
        staged.append({"staged": str(temporary), "final": str(final)})
    return staged


def publish_receipts(
    staged: list[dict[str, str]], receipts_dir: Path | None = None
) -> list[Path]:
    """Move staged receipts into place, never outside the receipts directory.

    os.replace overwrites whatever is at the destination, so when the paths
    came from a file on disk the destination is proved to be inside the
    receipts directory first.
    """
    published: list[Path] = []
    for item in staged:
        source, destination = Path(item["staged"]), Path(item["final"])
        if receipts_dir is not None and not (
            _under(source, receipts_dir) and _under(destination, receipts_dir)
        ):
            raise ConsumeError(
                f"REFUSING to publish {source} to {destination}: a receipt "
                f"path outside {receipts_dir} was not written by this tool"
            )
        _refuse_forged_overwrite(source, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        published.append(destination)
    return published


def _same_receipt(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Same receipt in substance: everything but when it was written down.

    Two runs recording the same answers produce different recorded_at stamps
    and identical everything else. A difference anywhere else means the two
    files disagree about what the reader answered.
    """
    ignored = {"recorded_at"}
    return {key: value for key, value in left.items() if key not in ignored} == {
        key: value for key, value in right.items() if key not in ignored
    }


def _refuse_forged_overwrite(source: Path, destination: Path) -> None:
    """Never write over a valid receipt with a different one.

    os.replace is silent and total. A receipt is the evidence that the reader
    answered one specific row, and the queue row stays closed whatever the
    receipt says afterwards, so an overwrite forges the record with nothing
    left to compare against. A file that is NOT a valid receipt is evidence of
    nothing, so a real receipt is still allowed to replace it.
    """
    if not destination.is_file():
        return
    try:
        existing = _read_json(destination)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(existing, dict):
        return
    page_id, item_id = existing.get("page_id"), existing.get("item_id")
    if not isinstance(page_id, str) or not isinstance(item_id, str):
        return
    try:
        current = read_receipt(destination, page_id, item_id)
    except ValidationError:
        return
    try:
        incoming = read_receipt(source, page_id, item_id)
    except ValidationError as exc:
        raise ConsumeError(
            f"REFUSING to publish {source} over {destination}: a valid receipt "
            f"for {page_id}/{item_id} is already there, and the incoming file "
            f"is not a receipt for that row ({exc}). Nothing was overwritten."
        ) from exc
    if not _same_receipt(current, incoming):
        raise ConsumeError(
            f"REFUSING to publish {source} over {destination}: a valid receipt "
            f"for {page_id}/{item_id} is already there and the incoming file "
            "records something different. The record of what the reader answered is "
            "not replaced by a claim about it. Nothing was overwritten; "
            "resolve this by hand."
        )


def _under(child: str | Path, parent: Path) -> bool:
    """True only if child, after every symlink is followed, is INSIDE parent.

    realpath is the point: a symlink or a .. segment that leaves the directory
    must fail this, because what follows a containment check here is rmtree and
    os.replace on the archive.
    """
    try:
        real_child = Path(os.path.realpath(str(child)))
        real_parent = Path(os.path.realpath(str(parent)))
    except (OSError, ValueError):
        return False
    return real_child != real_parent and real_child.is_relative_to(real_parent)


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.realpath(str(left)) == os.path.realpath(str(right))
    except (OSError, ValueError):
        return False


def _refuse(path: Path, reason: str) -> "ConsumeError":
    return ConsumeError(
        f"REFUSING to act on {path}: {reason}. A journal is a claim, not an "
        "instruction; nothing was published and nothing was deleted. Resolve "
        "this by hand."
    )


def validate_journal(
    path: Path, journal: Any, receipts_dir: Path, queue_path: Path
) -> dict[str, Any]:
    """Prove a journal describes THIS tool, THIS run and THIS queue.

    Everything downstream of here deletes directories and moves files over
    existing ones from paths the file itself supplies. Any journal-*.json a
    process can write would otherwise be an instruction to delete an arbitrary
    directory: the archive of the reader's handwriting is one such directory, and a
    forged journal did delete a note from it. So the whole file is checked
    before any of it is used, and every path is required to resolve strictly
    beneath the receipts directory.
    """
    if not isinstance(journal, dict):
        raise _refuse(path, "it is not a journal object")
    if journal.get("tool") != "consume_decisions":
        raise _refuse(path, "it was not written by this tool")
    if journal.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise _refuse(
            path,
            f"it has journal schema {journal.get('schema_version')!r}, "
            f"expected {JOURNAL_SCHEMA_VERSION}",
        )
    run_id = journal.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _refuse(path, "it names no run_id")
    if path.name != f"journal-{run_id}.json":
        raise _refuse(
            path, f"it claims run_id {run_id!r}, which is not the run this file names"
        )
    state = journal.get("state")
    if state not in JOURNAL_STATES:
        raise _refuse(path, f"its state {state!r} is not one this tool writes")
    claimed_queue = journal.get("queue_path")
    if not isinstance(claimed_queue, str) or not _same_path(claimed_queue, queue_path):
        raise _refuse(
            path,
            f"it belongs to the queue at {claimed_queue!r}, not {queue_path}",
        )
    for field in ("queue_sha_before", "queue_sha_after"):
        value = journal.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise _refuse(path, f"its {field} is not a sha256 digest")
    page_id = journal.get("page_id", "")
    if not isinstance(page_id, str):
        raise _refuse(path, "its page_id is not a string")

    staging = journal.get("staging")
    expected_staging = receipts_dir / f"staging-{run_id}"
    if not isinstance(staging, str):
        raise _refuse(path, "it names no staging directory")
    if not _under(staging, receipts_dir):
        raise _refuse(
            path,
            f"its staging directory {staging!r} does not resolve beneath the "
            f"receipts directory {receipts_dir}",
        )
    if Path(staging) != expected_staging:
        raise _refuse(
            path,
            f"its staging directory {staging!r} is not this run's "
            f"({expected_staging})",
        )

    receipts = journal.get("receipts")
    if not isinstance(receipts, list):
        raise _refuse(path, "its receipts are not a list")
    for item in receipts:
        if not isinstance(item, dict):
            raise _refuse(path, "one of its receipts is not an object")
        for field in ("staged", "final"):
            value = item.get(field)
            if not isinstance(value, str):
                raise _refuse(path, f"a receipt has no {field} path")
            if not _under(value, receipts_dir):
                raise _refuse(
                    path,
                    f"the {field} path {value!r} does not resolve beneath the "
                    f"receipts directory {receipts_dir}",
                )
        if not _under(item["staged"], expected_staging):
            raise _refuse(
                path,
                f"the staged path {item['staged']!r} is not inside this run's "
                f"staging directory",
            )
        if Path(item["final"]).parent != receipts_dir:
            raise _refuse(
                path,
                f"the final path {item['final']!r} is not a receipt in "
                f"{receipts_dir}",
            )
        # Containment is not correspondence. A staged file that is merely
        # somewhere inside the staging directory could be published over ANY
        # receipt the journal chose to name, which is how a contained file
        # called other-name.json overwrote a real receipt with forged content.
        # Both paths must be exactly the two this tool derives from the row's
        # own identity: <page_id>.<item_id>.json, in staging and in receipts.
        name = Path(item["final"]).name
        item_id = _receipt_item_id(name, page_id)
        if item_id is None:
            raise _refuse(
                path,
                f"the receipt name {name!r} is not <page>.<item>.json for the "
                f"page {page_id!r} this journal names",
            )
        if Path(item["staged"]).name != name:
            raise _refuse(
                path,
                f"its staged file {Path(item['staged']).name!r} does not carry "
                f"the name of the receipt {name!r} it claims to publish",
            )
        if not _same_path(item["final"], receipt_path(receipts_dir, page_id, item_id)):
            raise _refuse(
                path,
                f"the final path {item['final']!r} is not the receipt path for "
                f"{page_id}/{item_id}",
            )
        if not _same_path(item["staged"], expected_staging / name):
            raise _refuse(
                path,
                f"the staged path {item['staged']!r} is not {name} in this "
                f"run's staging directory",
            )
    return journal


def _receipt_item_id(name: str, page_id: str) -> str | None:
    """The item_id a receipt filename encodes, or None if it encodes none."""
    suffix = ".json"
    prefix = f"{page_id}."
    if not page_id or not name.startswith(prefix) or not name.endswith(suffix):
        return None
    item_id = name[len(prefix) : -len(suffix)]
    return item_id or None


def recover_journals(receipts_dir: Path, queue_path: Path) -> list[str]:
    """Finish or discard any run that died mid-write. Evidence, never guesswork.

    The journal records the queue hash before the write and the hash the write
    intended to produce. Comparing those against the live file says without
    ambiguity whether the queue change landed, so the receipts are either
    published to match it or thrown away. Every journal is validated in full
    first: an unvalidated one is a stranger's instruction to delete a
    directory.
    """
    messages: list[str] = []
    if not receipts_dir.is_dir():
        return messages
    for path in sorted(receipts_dir.glob("journal-*.json")):
        try:
            raw = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConsumeError(
                f"{path} is an unreadable journal ({exc}); a previous run's "
                "state cannot be determined, so nothing is written"
            ) from exc
        journal = validate_journal(path, raw, receipts_dir, queue_path)
        staged = journal.get("receipts", [])
        state = journal.get("state")
        if state == "published":
            # Even a finished journal is only cleaned up once every receipt it
            # declares is proved to be on disk. Deleting it otherwise deletes
            # the last trace that a closed row has no evidence behind it.
            for item in staged:
                _prove_declared_receipt(item, journal, receipts_dir, path)
            _discard_staging(journal, path, receipts_dir)
            messages.append(f"cleaned up a finished journal: {path.name}")
            continue
        live_sha = (
            tp.sha256_bytes(queue_path.read_bytes()) if queue_path.is_file() else ""
        )
        if live_sha == journal.get("queue_sha_after"):
            # The queue change landed, so the receipts MUST land with it.
            # Every declared receipt is proved to exist first - as a valid
            # staged file or as a valid final one - because the next steps
            # delete the staging directory and the journal, and a missing
            # receipt after that is a row closed with no record of the answer
            # and no record that anything went wrong (mc-63x2).
            to_publish: list[dict[str, str]] = []
            already = 0
            for item in staged:
                where = _prove_declared_receipt(item, journal, receipts_dir, path)
                if where == "staged":
                    to_publish.append(item)
                else:
                    already += 1
            published = publish_receipts(to_publish, receipts_dir)
            journal["state"] = "published"
            tp.atomic_json(path, journal)
            _discard_staging(journal, path, receipts_dir)
            messages.append(
                f"recovered {path.name}: the queue change had landed, so "
                f"{len(published)} receipts were published to match it"
                + (f" ({already} were already in place)" if already else "")
            )
        elif live_sha == journal.get("queue_sha_before"):
            _discard_staging(journal, path, receipts_dir)
            messages.append(
                f"recovered {path.name}: the queue change had NOT landed, so "
                f"{len(staged)} staged receipts were discarded"
            )
        else:
            raise ConsumeError(
                f"{path} describes a run that cannot be reconciled: the queue "
                f"is at {live_sha[:12]}, which is neither the state before "
                f"({str(journal.get('queue_sha_before'))[:12]}) nor after "
                f"({str(journal.get('queue_sha_after'))[:12]}) that run. "
                "Nothing is written. Resolve this by hand."
            )
    return messages


def _prove_declared_receipt(
    item: dict[str, str],
    journal: dict[str, Any],
    receipts_dir: Path,
    journal_path: Path,
) -> str:
    """Prove one receipt a journal declares really exists, and say where.

    Returns "staged" when a valid staged receipt is waiting to be published,
    or "final" when the receipt is already published and valid.

    Nothing here is taken on the journal's word. The staged file is read and
    validated against the page and row the journal names, so a shape-valid
    journal cannot publish arbitrary content; and if the staged file is gone,
    the FINAL receipt must already be a valid one for that row. When neither
    is true this refuses, leaving the journal and the staging directory where
    they are: an inconsistency you can still see beats an inconsistency whose
    only evidence was deleted to tidy up.
    """
    page_id = journal.get("page_id", "")
    if not isinstance(page_id, str):
        raise _refuse(journal_path, "its page_id is not a string")
    name = Path(item["final"]).name
    item_id = _receipt_item_id(name, page_id)
    if item_id is None:
        raise _refuse(journal_path, f"the receipt name {name!r} names no row")
    # Both paths are DERIVED from the row's own identity here, not taken from
    # the journal's word for them.
    final_path = receipt_path(receipts_dir, page_id, item_id)
    staged_path = Path(str(journal.get("staging", ""))) / final_path.name

    staged_receipt: dict[str, Any] | None = None
    if staged_path.is_file():
        try:
            staged_receipt = read_receipt(staged_path, page_id, item_id)
        except ValidationError as exc:
            raise _refuse(
                journal_path,
                f"its staged file {name} is not a valid receipt for "
                f"{page_id}/{item_id} ({exc})",
            ) from exc

    final_receipt: dict[str, Any] | None = None
    if final_path.is_file():
        try:
            final_receipt = read_receipt(final_path, page_id, item_id)
        except ValidationError:
            # Not a receipt, so not evidence. A valid staged one may replace
            # it; if there is no staged one, the check below refuses.
            final_receipt = None

    if staged_receipt is not None:
        if final_receipt is not None and not _same_receipt(
            final_receipt, staged_receipt
        ):
            raise _refuse(
                journal_path,
                f"publishing its staged {name} would overwrite the different, "
                f"valid receipt already recorded for {page_id}/{item_id}",
            )
        return "staged"
    if final_receipt is not None:
        return "final"
    raise _refuse(
        journal_path,
        f"the receipt it declares for {page_id}/{item_id} is MISSING: neither "
        f"{staged_path} nor {final_path} holds a valid one, so a queue row may "
        "be closed with no record of the answer behind it. The journal and the "
        "staging directory are left in place as the evidence of that",
    )


def _discard_staging(
    journal: dict[str, Any], path: Path, receipts_dir: Path
) -> None:
    """Delete one run's staging directory, and never anything else.

    The containment check is repeated here rather than trusted from the
    caller: this is the line that calls rmtree, so this is the line that has
    to be certain the path came from inside the receipts directory.
    """
    staging = journal.get("staging")
    if staging:
        if not _under(staging, receipts_dir):
            raise _refuse(
                path,
                f"its staging directory {staging!r} does not resolve beneath "
                f"{receipts_dir}, so nothing was deleted",
            )
        if not Path(staging).is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _segment_hits_rect(
    start: tuple[float, float], end: tuple[float, float], rect: list[float]
) -> bool:
    """Liang-Barsky: does the segment touch the rectangle at all?"""
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    low, high = 0.0, 1.0
    for delta, position, lower, upper in (
        (-dx, x0 - rect[0], None, None),
        (dx, rect[2] - x0, None, None),
        (-dy, y0 - rect[1], None, None),
        (dy, rect[3] - y0, None, None),
    ):
        if delta == 0:
            if position < 0:
                return False
            continue
        t = position / delta
        if delta < 0:
            low = max(low, t)
        else:
            high = min(high, t)
        if low > high:
            return False
    return True


def _polyline_hits(points: list[tuple[float, float]], rect: list[float]) -> bool:
    if not points:
        return False
    if len(points) == 1:
        return tp._inside(points[0][0], points[0][1], rect)
    return any(
        _segment_hits_rect(points[index], points[index + 1], rect)
        for index in range(len(points) - 1)
    )


class RowPlan:
    """What this run intends to do with one queue row's answers."""

    def __init__(
        self,
        item_id: str,
        title: str,
        answers: list[dict[str, Any]],
    ) -> None:
        self.item_id = item_id
        self.title = title
        self.answers = answers
        self.hold_reasons: list[str] = []
        self.status = "record"  # record | hold | already_recorded
        self.disposition = ""
        self.raw_ask = ""

    @property
    def held(self) -> bool:
        return self.status == "hold"

    def hold(self, reason: str) -> None:
        if reason not in self.hold_reasons:
            self.hold_reasons.append(reason)
        self.status = "hold"

    def answer_lines(self) -> list[str]:
        return [
            f"- **{answer['choice']}** {answer['label']} - row: {self.title}"
            for answer in self.answers
        ]

    def hold_text(self) -> str:
        return "; ".join(HOLD_REASONS.get(r, r) for r in self.hold_reasons)


def _answered_both_ways(answers: list[dict[str, Any]]) -> bool:
    """True when ONE option carries both a YES and a NO mark.

    Exclusivity is NOT inferred from card structure. Cards routinely present
    cumulative options: on 2026-08-16 card-001 asked "Clear the old test
    copies?" and "Those and the duplicate model files?", and card-009 asked
    "One-time refresh now?" and "Also authorize the scheduler design?" with
    "Accept staleness through the week?" marked NO. Two YES marks there are a
    coherent answer, not a contradiction.

    Document validation already fails a resolution that repeats an entry, so
    this is the last line of defence rather than the first.
    """
    per_entry: dict[str, set[str]] = {}
    for answer in answers:
        entry_id = str(answer.get("entry_id", ""))
        per_entry.setdefault(entry_id, set()).add(
            str(answer.get("choice", "")).upper()
        )
    return any(len(choices) > 1 for choices in per_entry.values())


def plan_document(
    document: Document,
    queue_rows: list[dict[str, str]],
    raw_rows: dict[str, str],
    receipts_dir: Path,
    recorded_receipts: set[tuple[str, str]] | None = None,
) -> tuple[list[RowPlan], list[dict[str, Any]]]:
    """Decide, without writing anything, what happens to each answered row."""
    recorded_receipts = recorded_receipts or set()
    rows_by_id = {row["item_id"]: row for row in queue_rows}
    touched, blocking_ambiguities, _notes = document.ambiguous_items()

    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for decision in document.decisions:
        item_id = str(decision.get("item_id", ""))
        if not item_id:
            continue
        grouped.setdefault(item_id, []).append(decision)

    plans: list[RowPlan] = []
    for item_id, answers in grouped.items():
        answers = sorted(answers, key=lambda a: (a.get("decision_number", 0)))
        row = rows_by_id.get(item_id)
        title = (
            row["title"]
            if row
            else document.item_headlines.get(item_id, f"unknown row {item_id}")
        )
        plan = RowPlan(item_id, title, answers)
        # Carry the Ask from the Active row as written. Reading it back out of
        # the Closed row truncates it at the first pipe, because that table has
        # three columns and an Ask may contain pipes.
        plan.raw_ask = raw_rows.get(item_id, "")

        key = (document.page_id, item_id)
        existing = receipt_path(receipts_dir, document.page_id, item_id)
        if key in recorded_receipts:
            plan.status = "already_recorded"
            plans.append(plan)
            continue
        if existing.exists():
            try:
                receipt = read_receipt(existing, document.page_id, item_id)
            except ValidationError as exc:
                plan.hold(f"{HOLD_REASONS['receipt_unreadable']} ({exc})")
                plans.append(plan)
                continue
            plan.title = receipt.get("row_title", plan.title)
            plan.status = "already_recorded"
            plans.append(plan)
            continue

        if blocking_ambiguities:
            plan.hold("document_blocked")
        if item_id in touched:
            plan.hold("ambiguity_touches_row")
        if _answered_both_ways(answers):
            plan.hold("entry_answered_both_ways")
        if row is None:
            plan.hold("row_not_in_active")

        if not plan.held:
            chosen = "; ".join(
                f"{answer['choice']} \"{answer['label']}\"" for answer in answers
            )
            plan.disposition = (
                f"PEN {document.date}: {chosen}. Recorded from the "
                f"{document.date} decisions document by consume_decisions on "
                f"{dt.date.today().isoformat()}."
            )
        plans.append(plan)
    return plans, blocking_ambiguities


# ---------------------------------------------------------------------------
# Queue writing (always on a working copy, validated before it lands)
# ---------------------------------------------------------------------------


def _closed_separator_index(lines: list[str]) -> int | None:
    closed = next(
        (index for index, line in enumerate(lines) if line.startswith("## Closed")),
        None,
    )
    if closed is None:
        return None
    return next(
        (
            index
            for index in range(closed + 1, min(closed + 8, len(lines)))
            if lines[index].startswith("|---")
        ),
        None,
    )


def rewrite_inserted_disposition(
    path: Path, title: str, disposition: str, raw_ask: str
) -> None:
    """Replace the disposition cell of the row close_review_queue_row just added.

    close_review_queue_row writes a fixed "approved" sentence, so the freshly
    inserted Closed row (always the first data row under the Closed separator)
    is rewritten in place here. The original Ask is carried in from the Active
    row rather than recovered from the row just written, which would truncate
    it at its first pipe.

    The cell is escaped on the way in. The Closed table has three columns and
    tp._split_row splits on a raw pipe, so an Ask containing one pipe made a
    four-cell row: the disposition then read as the wrong column wherever the
    queue is rendered or linted. The byte-exact Ask is carried outside the
    table, in the Pen dispositions section, where a pipe means nothing.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    separator = _closed_separator_index(lines)
    if separator is None or separator + 1 >= len(lines):
        raise ConsumeError("The Closed table could not be found after closing a row.")
    cells = tp._split_row(lines[separator + 1])
    if cells is None or len(cells) < 3:
        raise ConsumeError("The row just closed is not where it was expected.")
    if tp.clean_markdown(cells[0]) != tp.clean_markdown(title):
        raise ConsumeError(
            "The row just closed does not match the row that was requested."
        )
    text = disposition
    if raw_ask:
        text = f"{disposition} Original ask: {raw_ask}"
    row_cells = [
        tp.escape_table_cell(cells[0]),
        tp.escape_table_cell(cells[1]),
        tp.escape_table_cell(text),
    ]
    lines[separator + 1] = "| " + " | ".join(row_cells) + " |"
    written = tp._split_row(lines[separator + 1])
    if written is None or len(written) != 3:
        raise ConsumeError(
            "the rewritten Closed row would not parse as three cells; the "
            "change was discarded"
        )
    tp.atomic_text(path, "\n".join(lines) + "\n")


def _section_insert_index(lines: list[str]) -> int:
    """Where a new Pen dispositions section goes: after the newest existing one."""
    last = None
    for index, line in enumerate(lines):
        if line.startswith("## Pen dispositions"):
            last = index
    if last is not None:
        for index in range(last + 1, len(lines)):
            if lines[index].startswith("## "):
                return index
        return len(lines)
    closed = next(
        (index for index, line in enumerate(lines) if line.startswith("## Closed")),
        None,
    )
    return closed if closed is not None else len(lines)


def insert_disposition_section(
    path: Path, document: Document, plans: list[RowPlan]
) -> None:
    """Append the durable record of every answer this run recorded."""
    recorded = [plan for plan in plans if plan.status == "record"]
    if not recorded:
        return
    held = [plan for plan in plans if plan.held]
    lines = path.read_text(encoding="utf-8").splitlines()
    index = _section_insert_index(lines)
    block = [
        f"## Pen dispositions - {document.date} "
        f"(applied by consume_decisions {dt.date.today().isoformat()})",
        "",
        f"Read from `{document.resolution_path}` (page `{document.page_id}`). "
        f"{len(recorded)} queue rows recorded, {len(held)} held for review. "
        "One line per answered option.",
        "",
    ]
    for plan in recorded:
        block.extend(plan.answer_lines())
        if plan.raw_ask:
            # Outside the table, so the Ask keeps its own bytes: pipes,
            # spacing and all. The Closed cell carries an escaped copy.
            block.append(f"  - original ask, as written: {plan.raw_ask}")
    if held:
        block.append("")
        block.append("Held, still Active, not recorded:")
        for plan in held:
            block.append(f"- {plan.title} - {plan.hold_text()}")
    block.append("")
    lines[index:index] = block
    tp.atomic_text(path, "\n".join(lines) + "\n")


class DocumentResult:
    def __init__(self, document: Document) -> None:
        self.document = document
        self.plans: list[RowPlan] = []
        self.orphan_ambiguities: list[dict[str, Any]] = []
        self.error: str | None = None
        self.reresolved_path: Path | None = None
        self.receipts_published: list[Path] = []
        self.queue_committed = False
        self.skipped: str | None = None
        self.notes: list[NoteRecord] = []

    @property
    def recorded(self) -> list[RowPlan]:
        return [plan for plan in self.plans if plan.status == "record"]

    @property
    def held(self) -> list[RowPlan]:
        return [plan for plan in self.plans if plan.held]

    @property
    def already(self) -> list[RowPlan]:
        return [plan for plan in self.plans if plan.status == "already_recorded"]


def process_document(
    document: Document,
    working_queue: Path,
    receipts_dir: Path,
    recorded_receipts: set[tuple[str, str]],
) -> DocumentResult:
    """Run one document against the working queue copy. Never touches the real file."""
    result = DocumentResult(document)
    original = working_queue.read_bytes()
    try:
        document.validate()
    except UnsupportedDocumentError as exc:
        # Named and skipped, never fatal to the rest of the run.
        result.skipped = str(exc)
        return result
    except ValidationError as exc:
        result.error = f"the resolution is not trustworthy: {exc}"
        return result
    # Notes are collected whatever else happens to this document: handwriting
    # nobody has read is the thing most likely to be time-critical.
    result.notes = document.notes(
        {str(d.get("item_id", "")) for d in document.decisions}
    )
    try:
        rows_before = tp.parse_review_queue(working_queue, require_keys=False)
        raw_rows = active_raw_rows(working_queue)
    except Exception as exc:  # the queue is the acceptance test; refuse to guess
        result.error = f"the review queue does not parse before the change: {exc}"
        return result

    plans, orphans = plan_document(
        document, rows_before, raw_rows, receipts_dir, recorded_receipts
    )
    result.plans = plans
    result.orphan_ambiguities = orphans
    result.notes = document.notes(
        {plan.item_id for plan in plans}
    )
    if not plans:
        return result

    try:
        for plan in plans:
            if plan.status != "record":
                continue
            try:
                tp.close_review_queue_row(working_queue, plan.item_id)
            except tp.TodayPageError as exc:
                plan.hold(f"the queue row could not be closed: {exc}")
                continue
            rewrite_inserted_disposition(
                working_queue, plan.title, plan.disposition, plan.raw_ask
            )
        insert_disposition_section(working_queue, document, plans)

        rows_after = tp.parse_review_queue(working_queue, require_keys=False)
        expected = len(rows_before) - len(result.recorded)
        if len(rows_after) != expected:
            raise ConsumeError(
                f"the queue would hold {len(rows_after)} active rows, expected "
                f"{expected}; the change was discarded"
            )
        # The count alone is not enough: a corruption that drops one row and
        # invents another keeps the count. Every row that was not recorded has
        # to still be there, by id.
        expected_ids = {row["item_id"] for row in rows_before} - {
            plan.item_id for plan in result.recorded
        }
        actual_ids = {row["item_id"] for row in rows_after}
        if actual_ids != expected_ids:
            raise ConsumeError(
                "the queue's remaining active rows are not the ones expected; "
                "the change was discarded"
            )
    except Exception as exc:
        working_queue.write_bytes(original)
        result.error = str(exc)
        for plan in result.plans:
            if plan.status == "record":
                plan.status = "hold"
                plan.hold_reasons.append(f"run aborted: {exc}")
        return result
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(results: list[DocumentResult], apply: bool) -> str:
    out: list[str] = []
    mode = "APPLY" if apply else "DRY RUN"
    out.append(f"consume_decisions ({mode})")
    total_recorded = total_held = total_already = total_notes = 0
    skipped = 0
    for result in results:
        document = result.document
        out.append("")
        out.append(f"{document.date}  page {document.page_id}")
        if document.resolution_source == "re-resolved":
            detail = (
                f"  ink re-resolved with the current resolver; "
                f"ambiguities {len(document.stored_resolution.get('ambiguities', []))}"
                f" -> {len(document.ambiguities)}"
            )
            if document.resolution_changed:
                detail += ", ANSWERS DIFFER from the stored resolution"
            out.append(detail)
        else:
            out.append(f"  STORED RESOLUTION USED: {document.resolution_note}")
        if result.skipped:
            out.append(f"  SKIPPED: {result.skipped}")
            continue
        if result.error:
            out.append(f"  ERROR: {result.error}")
        if result.orphan_ambiguities:
            out.append(
                f"  DOCUMENT BLOCKED: {len(result.orphan_ambiguities)} unresolved "
                "marks cannot be attributed to any row, so nothing on this "
                "document is recorded:"
            )
            for ambiguity in result.orphan_ambiguities:
                out.append(f"    {describe_ambiguity(ambiguity)}")
        if result.notes:
            total_notes += len(result.notes)
            out.append(
                f"  NOTES TO READ: {len(result.notes)} cards carry the reader's "
                "handwriting. A human has to read these; this tool cannot."
            )
            for note in result.notes:
                out.append(f"    {note.describe()}")
        if not result.plans:
            out.append("  no decisions found")
            continue
        out.append(
            f"  rows answered: {len(result.plans)}   "
            f"would record: {len(result.recorded)}   "
            f"hold: {len(result.held)}   "
            f"already recorded: {len(result.already)}"
        )
        for plan in result.recorded:
            choices = ", ".join(
                f"{answer['choice']} {answer['label']}" for answer in plan.answers
            )
            out.append(f"    RECORD  {plan.title}")
            out.append(f"            {choices}")
        for plan in result.held:
            out.append(f"    HOLD    {plan.title}")
            out.append(f"            {plan.hold_text()}")
        for plan in result.already:
            out.append(f"    NOOP    {plan.title} (receipt already on disk)")
        total_recorded += len(result.recorded)
        total_held += len(result.held)
        total_already += len(result.already)
    skipped = sum(1 for result in results if result.skipped)
    out.append("")
    out.append(
        f"TOTAL  recorded: {total_recorded}  held: {total_held}  "
        f"already recorded: {total_already}  notes to read: {total_notes}  "
        f"documents skipped: {skipped}"
    )
    if total_notes:
        # Recording answers while handwriting sits unread is not success.
        out.append(
            f"NOT DONE: {total_notes} cards carry handwriting nobody has read. "
            "Open the images listed above."
        )
    if total_recorded == 0 and total_held == 0:
        if total_already:
            out.append(
                "nothing to do: every answer on these documents was already "
                "recorded"
            )
        else:
            out.append("no decisions found")
    return "\n".join(out)


def committed_rows(results: list[DocumentResult]) -> int:
    """Rows whose queue change actually landed. Never rows that were planned.

    A document that died in staging has plans still marked "record": they were
    intended, not recorded. Counting them told the reader a row was handled when the
    queue had not moved at all.
    """
    return sum(len(r.recorded) for r in results if r.queue_committed)


def run_failure(results: list[DocumentResult], failure: str | None = None) -> str | None:
    """The one thing that went wrong, run-level or document-level.

    A per-document error never reached main, so a failed apply could be
    announced with the success sentence. Any error at all is a failure.
    """
    if failure:
        return failure
    for result in results:
        if result.error:
            return f"{result.document.date}: {result.error}"
    return None


def alert_body(results: list[DocumentResult], failure: str | None = None) -> str:
    lines: list[str] = []
    failure = run_failure(results, failure)
    recorded = committed_rows(results)
    held = sum(len(r.held) for r in results)
    queue_changed = any(r.queue_committed for r in results)
    if failure:
        lines.append(f"Pen answer run FAILED: {failure}")
        if queue_changed:
            published = sum(len(r.receipts_published) for r in results)
            lines.append(
                f"The queue WAS changed and {published} receipts were "
                "published; check the dispositions section."
                if published
                else "The queue WAS changed but the receipts did NOT publish; "
                "a journal is on disk and the next run will finish it."
            )
        else:
            lines.append("No queue change was made.")
    else:
        lines.append(
            f"Pen answers consumed: {recorded} queue rows recorded, {held} held. "
            "Counts are what landed in the queue, not what was planned."
        )
        if not queue_changed:
            lines.append("No queue change was made.")
    notes = sum(len(r.notes) for r in results)
    if notes:
        lines.append(
            f"UNREAD: {notes} cards carry the reader's handwriting that nobody has "
            "read. This is not a finished run."
        )
    for result in results:
        for note in result.notes:
            lines.append(f"NOTE {result.document.date}: {note.describe()}")
        if result.skipped:
            lines.append(f"SKIPPED {result.document.date}: {result.skipped}")
        if result.orphan_ambiguities:
            lines.append(
                f"BLOCKED {result.document.date}: "
                f"{len(result.orphan_ambiguities)} unattributable marks"
            )
        for plan in result.recorded:
            lines.append(f"RECORDED {result.document.date}: {plan.title}")
        for plan in result.held:
            lines.append(
                f"HELD {result.document.date}: {plan.title} - {plan.hold_text()}"
            )
        if result.error:
            lines.append(f"ERROR {result.document.date}: {result.error}")
    return "\n".join(lines)


def send_alert(subject: str, body: str, slug: str = "pen-consumer") -> None:
    """Send one bus alert through the existing helper. Loud failure, never silent."""
    if not BUS_ALERT.is_file():
        raise ConsumeError(f"the alert helper is missing: {BUS_ALERT}")
    completed = subprocess.run(
        [str(BUS_ALERT), subject, body, slug],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ConsumeError(
            f"the alert helper failed (exit {completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _apply_document(
    document: Document,
    result: DocumentResult,
    working_queue: Path,
    queue_path: Path,
    receipts_dir: Path,
    queue_sha: str,
    run_id: str,
) -> str:
    """Land one document's change: stage receipts, CAS the queue, publish.

    Returns the new live queue hash. A crash anywhere in here leaves a journal
    that recover_journals resolves from evidence on the next run.
    """
    new_text = working_queue.read_text(encoding="utf-8")
    intended_sha = tp.sha256_bytes(new_text.encode("utf-8"))
    receipts_dir.mkdir(parents=True, exist_ok=True)

    staged = stage_receipts(
        receipts_dir, run_id, document, result.recorded, intended_sha
    )
    journal = journal_path(receipts_dir, run_id)
    tp.atomic_json(
        journal,
        {
            "tool": "consume_decisions",
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "run_id": run_id,
            "state": "prepared",
            "queue_path": str(queue_path),
            "queue_sha_before": queue_sha,
            "queue_sha_after": intended_sha,
            "staging": str(receipts_dir / f"staging-{run_id}"),
            "receipts": staged,
            "page_id": document.page_id,
            "prepared_at": dt.datetime.now().astimezone().isoformat(),
        },
    )

    live_sha = commit_queue(queue_path, new_text, queue_sha)
    result.queue_committed = True

    record = _read_json(journal)
    record["state"] = "queue_written"
    tp.atomic_json(journal, record)

    result.receipts_published = publish_receipts(staged, receipts_dir)

    record["state"] = "published"
    tp.atomic_json(journal, record)
    _discard_staging(record, journal, receipts_dir)
    return live_sha


def run(
    processed_dirs: Iterable[Path],
    queue_path: Path,
    receipts_dir: Path,
    apply: bool,
    working_dir: Path | None = None,
) -> tuple[list[DocumentResult], int]:
    """Plan (and optionally apply) every document against one queue file."""
    documents = [Document(Path(path)) for path in processed_dirs]
    documents.sort(key=lambda doc: doc.date)

    if apply:
        with queue_lock(queue_path):
            recover_journals(receipts_dir, queue_path)
            return _run_documents(
                documents, queue_path, receipts_dir, True, working_dir
            )
    return _run_documents(documents, queue_path, receipts_dir, False, working_dir)


def _run_documents(
    documents: list[Document],
    queue_path: Path,
    receipts_dir: Path,
    apply: bool,
    working_dir: Path | None,
) -> tuple[list[DocumentResult], int]:
    working_root = working_dir or Path(
        tempfile.mkdtemp(prefix="consume-decisions-")
    )
    working_root.mkdir(parents=True, exist_ok=True)
    working_queue = working_root / "REVIEW-QUEUE.md"
    live_bytes = queue_path.read_bytes()
    working_queue.write_bytes(live_bytes)
    queue_sha = tp.sha256_bytes(live_bytes)
    run_id = f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    recorded_receipts: set[tuple[str, str]] = set()
    results: list[DocumentResult] = []
    exit_code = EXIT_OK
    for index, document in enumerate(documents):
        result = process_document(
            document, working_queue, receipts_dir, recorded_receipts
        )
        results.append(result)
        for plan in result.recorded:
            recorded_receipts.add((document.page_id, plan.item_id))
        if result.error:
            exit_code = EXIT_ERROR
        elif (
            result.held or result.orphan_ambiguities or result.notes
        ) and exit_code == EXIT_OK:
            # Unread handwriting is a reason to look, so the scheduler surfaces
            # it exactly as it surfaces a held row.
            exit_code = EXIT_HOLD

        if apply:
            # The refreshed resolution is saved next to the original, never
            # over it, so the record of what the old resolver said survives.
            saved = document.write_reresolved()
            if saved is not None:
                result.reresolved_path = saved
        if apply and result.recorded and not result.error:
            try:
                queue_sha = _apply_document(
                    document,
                    result,
                    working_queue,
                    queue_path,
                    receipts_dir,
                    queue_sha,
                    f"{run_id}-{index:02d}",
                )
            except Exception as exc:
                # Report which side of the queue write it died on. A crash
                # after the commit leaves a journal that the next run resolves.
                result.error = (
                    f"{exc} (the queue change HAD landed; receipts are "
                    "journalled for recovery)"
                    if result.queue_committed
                    else f"{exc} (no queue change was made)"
                )
                exit_code = EXIT_ERROR
                # Re-seed the working copy from the live file so later
                # documents in this run never build on an abandoned change.
                live = queue_path.read_bytes()
                working_queue.write_bytes(live)
                queue_sha = tp.sha256_bytes(live)
    return results, exit_code


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record the reader's resolved pen decisions as dispositions on the review "
            "queue. Dry run by default."
        )
    )
    parser.add_argument(
        "--processed-dir",
        action="append",
        default=[],
        help="a processed directory containing resolution.json (repeatable)",
    )
    parser.add_argument(
        "--all-archived",
        action="store_true",
        help=f"walk every archived processed directory under {TODAY_PAGES}",
    )
    parser.add_argument("--review-queue", default=str(REVIEW_QUEUE))
    parser.add_argument("--receipts-dir", default=str(RECEIPTS_DIR))
    parser.add_argument("--question-state", default=str(QUESTION_STATE))
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the queue and the receipts (default is dry run)",
    )
    parser.add_argument(
        "--partial-page-reviewed-separately",
        action="store_true",
        help="let the caller report marks still under review after applying schema-2 answers",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        help="skip the bus alert on an apply run (testing only)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    apply = bool(args.apply)
    queue_path = Path(args.review_queue).resolve()
    receipts_dir = Path(args.receipts_dir)
    question_state_path = Path(args.question_state).resolve()

    dirs = [Path(value) for value in args.processed_dir]
    if args.all_archived:
        dirs.extend(discover_processed_dirs())
    if not dirs:
        print(
            "consume_decisions: give --processed-dir PATH or --all-archived",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not queue_path.is_file():
        print(f"consume_decisions: no review queue at {queue_path}", file=sys.stderr)
        return EXIT_ERROR

    try:
        schema_versions = {
            _read_json(path / "resolution.json").get("schema_version") for path in dirs
        }
    except (OSError, json.JSONDecodeError) as exc:
        print(f"consume_decisions: cannot read a resolution: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if 2 in schema_versions:
        if schema_versions != {2}:
            print(
                "consume_decisions: schema-2 and legacy documents must be "
                "consumed in separate runs",
                file=sys.stderr,
            )
            return EXIT_ERROR
        if len(dirs) != 1:
            print(
                "consume_decisions: consume one schema-2 document at a time",
                file=sys.stderr,
            )
            return EXIT_ERROR
        try:
            from decision_consumer_v3 import consume as consume_v3
            from decision_consumer_v3 import render_report as render_v3_report

            document = Document(dirs[0])
            if document.manifest is None:
                raise ConsumeError(
                    f"no manifest matches schema-2 page {document.page_id}"
                )
            result = consume_v3(
                document.manifest,
                document.resolution,
                queue_path=queue_path,
                state_path=question_state_path,
                document_date=document.date,
                apply=apply,
                notifications_enabled=not args.no_alert,
                receipts_dir=receipts_dir,
                resolution_path=str(document.resolution_path),
                resolution_source=document.resolution_source,
            )
            print(render_v3_report(result))
            if result.notifications_failed:
                return EXIT_ERROR
            if result.clarification_entry_ids and not args.partial_page_reviewed_separately:
                return EXIT_HOLD
            return EXIT_OK
        except Exception as exc:
            print(f"consume_decisions: {exc}", file=sys.stderr)
            return EXIT_ERROR

    results: list[DocumentResult] = []
    failure: str | None = None
    exit_code = EXIT_OK
    try:
        results, exit_code = run(dirs, queue_path, receipts_dir, apply)
    except ConsumeError as exc:
        failure, exit_code = str(exc), EXIT_ERROR
    except Exception as exc:  # never leave a caller guessing
        failure, exit_code = f"unexpected failure: {exc}", EXIT_ERROR

    if results:
        print(render_report(results, apply))
    if failure:
        print(f"consume_decisions: {failure}", file=sys.stderr)

    if apply and not args.no_alert:
        # A failure is exactly when an alert matters most, so the alert goes
        # out on the failure paths too, saying whether the queue moved.
        # A document-level error is a failed run: the subject must never say
        # otherwise, and the counts must mean committed.
        reported_failure = run_failure(results, failure)
        recorded = committed_rows(results)
        held = sum(len(r.held) for r in results)
        subject = (
            f"Pen answer run FAILED: {reported_failure.splitlines()[0][:80]}"
            if reported_failure
            else (
                f"Pen answers recorded: {recorded} rows, {held} held"
                + (
                    f", {sum(len(r.notes) for r in results)} handwritten notes "
                    "UNREAD"
                    if any(r.notes for r in results)
                    else ""
                )
            )
        )
        try:
            send_alert(subject, alert_body(results, failure))
        except Exception as exc:
            print(
                "consume_decisions: ALERT DELIVERY FAILED, nobody was told: "
                f"{exc}",
                file=sys.stderr,
            )
            return EXIT_ERROR
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
