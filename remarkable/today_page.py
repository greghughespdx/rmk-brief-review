#!/usr/bin/env python3
"""Generate and resolve the reMarkable Decisions document.

The PDF and its JSON manifest are one versioned artifact. The manifest carries
only typed, allowlisted actions. Ink resolution is pure and deterministic.
Execution is a separate, explicit step and defaults to a dry run.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

PAGE_W = 612.0
PAGE_H = 792.0
# mc-iacbw: schema 2 is the v3 Decisions document. Every question declares its
# own answer form, every question carries Defer and Discard, the supporting
# document sits behind its card, and page 1 says what was held back.
SCHEMA_VERSION = 2
# Schema versions validate_manifest will still read. Archived schema-1
# manifests must keep validating so ink drawn on an older sheet can still be
# replayed through resolve(); only generation moves to the current version.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
# The versions the v2 documents were written under. Legacy code and archived
# fixtures need to name "the version before v3" without meaning "whatever
# today_page currently generates", which is what SCHEMA_VERSION and
# LAYOUT_VERSION now mean (mc-iacbw: they moved to 2 and decisions-v3.0, and
# every legacy reader that had been reading them as constants broke).
LEGACY_SCHEMA_VERSION = 1
# mc-vjz5: bumped from v2.2. Appendix pages now hold styled markdown line
# records (heading/body/bullet/code/quote/rule) with variable line heights
# instead of fixed-count plain-text lines, which changes appendix pagination.
LAYOUT_VERSION = "decisions-v3.0"
# mc-vjz5: layout versions that consume_decisions.py may still read and
# replay answers from, even though LAYOUT_VERSION has moved on. A document is
# safe to include here only when every change since it was current left
# answer-box geometry, card layout, and ink resolution untouched -- v2.1 to
# v2.3 changed handwriting note-line count/spacing and appendix-page markdown
# typography only, and moved no YES/NO box, so ink resolution across them is
# unaffected. This is narrower than "every version this tool has ever used":
# a version whose change did touch answer-box geometry or ink resolution must
# never be added here (consume_decisions.py raises UnsupportedDocumentError
# for anything outside this set, which is the point of the check).
ANSWER_COMPATIBLE_LAYOUT_VERSIONS = frozenset(
    {"decisions-v2.1", "decisions-v2.2", "decisions-v2.3"}
)
# mc-iacbw: every layout validate_manifest will read. decisions-v3.0 is NOT in
# ANSWER_COMPATIBLE_LAYOUT_VERSIONS on purpose: v3 moved every answer box and
# added Defer, Discard, lettered choices and write-in lines, so a consumer
# written for the v2 sheets must refuse a v3 document rather than read the new
# boxes with old assumptions. The v3 consumer adds itself to that set when it
# is ready (owner: the resolver/consumer side of mc-iacbw).
SUPPORTED_LAYOUT_VERSIONS = ANSWER_COMPATIBLE_LAYOUT_VERSIONS | {"decisions-v3.0"}
LEGACY_LAYOUT_VERSION = "decisions-v2.3"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rmw_config  # noqa: E402

_CONFIG = rmw_config.load()
# The content root: Read:, Facts: and Specimen: paths on a card resolve under
# it (paths.content_root in the config; by default the queue file's folder).
# The name is historical; it no longer has to be a git repository.
REPO_ROOT = _CONFIG.content_root
# Answer-box geometry (mc-rrjv). Every number here was measured off the reader's own
# ink, not chosen for looks. Across every archived document his tick marks run
# 15.5-21.7 points tall (median), so a 13.0-point box was ALWAYS overhung: the
# mark spilled into the vertically adjacent question's 4.0-point tolerance
# margin and the resolver quarantined a mark that touched exactly one real box
# and was centred in it. 24.0-point boxes on a 44.0-point minimum row pitch put
# 12.0 points of dead space between one row's hit region and the next, so a
# 21.7-point mark centred on its box cannot reach its neighbour. The 52.0-point
# YES/NO spacing and the 4.0-point tolerance are unchanged - they were never
# the problem, and changing them would move answers that already read correctly.
BOX_SIZE = 24.0
BOX_TOLERANCE = 4.0
# In v3 this is the least vertical distance between one question's row of
# answer boxes and the next question's. The horizontal pitch within a row is
# ANSWER_BOX_PITCH; the old CHOICE_PITCH described a YES/NO pair that no
# longer exists, and reporting it in the manifest geometry would have been a
# stale number in a load-bearing place.
MIN_ROW_PITCH = 44.0
GIANT_MARK_SIZE = 90.0
PAGE_MARGIN_X = 30.0
PAGE_CONTENT_TOP = PAGE_H - 82.0
PAGE_CONTENT_BOTTOM = 30.0
CARD_GAP = 8.0
CARD_PADDING = 10.0
QUESTION_FONT_SIZE = 12.0
BODY_FONT_SIZE = 11.0
QUESTION_LINE_HEIGHT = 15.0
BODY_LINE_HEIGHT = 14.0
MAX_SUBDECISIONS = 26

# --- v3 answer controls (mc-iacbw) --------------------------------------
# Every question prints its own row of answer boxes under its text: the
# answer boxes for its form (Y/N, or A..E), then a wider gap, then Defer and
# Discard. ANSWER_BOX_PITCH is centre to centre. It has to stay larger than
# BOX_SIZE + 2 * BOX_TOLERANCE (24 + 8 = 32) or two boxes' hit regions would
# overlap and a single tick could read as two answers; _assert_box_separation
# proves that on every rendered document rather than trusting this number.
ANSWER_BOX_PITCH = 48.0
ANSWER_GROUP_GAP = 34.0
BOX_LABEL_FONT_SIZE = 7.5
BOX_LABEL_GAP = 10.0
QUESTION_TEXT_GAP = 6.0
QUESTION_BLOCK_GAP = 14.0
# mc-bcsrx: the reader's pen needs room. A single "Or write in:" line left the
# bottom third of a page blank while a multi-leg card's write-in choice
# (e.g. "a different limit I write in") had exactly one line to answer on
# (pen note, review package page 31, 2026-09-20: "2x to 3x the pen writing
# area on future documents"). Three ruled lines, generously spaced for a
# pen, replace the single line. WRITE_IN_ROW_HEIGHT is derived from the same
# three constants _place_question_geometry uses to place the lines, so the
# space _question_block reserves and the geometry actually drawn can never
# drift apart.
WRITE_IN_LINE_COUNT = 3
WRITE_IN_TOP_GAP = 16.0
WRITE_IN_LINE_GAP = 32.0
WRITE_IN_BOTTOM_GAP = 12.0
WRITE_IN_ROW_HEIGHT = (
    WRITE_IN_TOP_GAP
    + (WRITE_IN_LINE_COUNT - 1) * WRITE_IN_LINE_GAP
    + WRITE_IN_BOTTOM_GAP
)
WRITE_IN_LABEL = "Or write in:"
MAX_CHOICE_OPTIONS = 5
CHOICE_LETTERS = "ABCDE"
DEFER_LABEL = "Defer"
DISCARD_LABEL = "Discard"
# The two file kinds a Read document may be. Anything else holds the row: a
# binary or an unknown format cannot be rendered into readable pages.
READ_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".text"})
COVER_HEADING_GAP = 22.0
APPENDIX_FONT_SIZE = 9.5
APPENDIX_LINE_HEIGHT = 12.0
APPENDIX_SPECIMEN_SIZE = 18.0
APPENDIX_SPECIMEN_HEIGHT = 43.0
_SPECIMEN_RE = re.compile(r"^Specimen:\s*(\S+)\s*\|\s*(.+?)\s*$")
# mc-vjz5: appendix markdown-rendering layout. Indent unit is per nesting
# level (bullets/quotes); marker gap is the hanging-indent reserved for a
# bullet/number glyph so wrapped continuation lines align under the text,
# not under the marker. Block/heading gaps are the "blank" spacer records
# inserted between blocks - headings get a taller gap above them.
APPENDIX_INDENT_UNIT = 14.0
APPENDIX_MARKER_GAP = 16.0
APPENDIX_BLOCK_GAP = 6.0
APPENDIX_HEADING_GAP = 11.0
# mc-dsb68: real table layout. A pipe table is laid out in columns measured
# with the same reportlab string widths the rest of the appendix uses, with a
# gap between columns, a little breathing room under each row's last line, and
# a rule drawn in that padding. A table too wide to give every column its
# widest single word degrades to labelled blocks rather than overflowing.
APPENDIX_TABLE_COL_GAP = 8.0
APPENDIX_TABLE_ROW_PAD = 4.0
APPENDIX_TABLE_LINE_HEIGHT = APPENDIX_LINE_HEIGHT - 1.0
APPENDIX_TABLE_MIN_COL_WIDTH = 26.0
_APPENDIX_STYLE_FONTS: dict[str, tuple[str, float]] = {
    "h1": ("Helvetica-Bold", 16.0),
    "h2": ("Helvetica-Bold", 14.0),
    "h3": ("Helvetica-Bold", 12.5),
    "h4": ("Helvetica-Bold", 11.5),
    "h5": ("Helvetica-Bold", APPENDIX_FONT_SIZE + 1.0),
    "h6": ("Helvetica-Bold", APPENDIX_FONT_SIZE),
    "body": ("Helvetica", APPENDIX_FONT_SIZE),
    "bullet": ("Helvetica", APPENDIX_FONT_SIZE),
    "quote": ("Helvetica-Oblique", APPENDIX_FONT_SIZE),
    "quote_label": ("Helvetica-Bold", APPENDIX_FONT_SIZE),
    "code": ("Courier", APPENDIX_FONT_SIZE),
    # Proportional, not Courier (mc-dsb68): a table is now laid out in real
    # columns, so the monospace grid that once faked alignment costs width
    # for nothing.
    "table": ("Helvetica", APPENDIX_FONT_SIZE),
    "table_header": ("Helvetica-Bold", APPENDIX_FONT_SIZE),
    "link": ("Helvetica", APPENDIX_FONT_SIZE - 0.5),
    "rule": ("Helvetica", APPENDIX_FONT_SIZE),
}
_APPENDIX_STYLE_HEIGHTS: dict[str, float] = {
    "h1": 21.0,
    "h2": 18.5,
    "h3": 16.5,
    "h4": 15.0,
    "h5": 13.5,
    "h6": APPENDIX_LINE_HEIGHT,
    "body": APPENDIX_LINE_HEIGHT,
    "bullet": APPENDIX_LINE_HEIGHT,
    "quote": APPENDIX_LINE_HEIGHT,
    "quote_label": APPENDIX_LINE_HEIGHT,
    "code": APPENDIX_LINE_HEIGHT - 1.0,
    "table": APPENDIX_TABLE_LINE_HEIGHT,
    "table_header": APPENDIX_TABLE_LINE_HEIGHT,
    "link": APPENDIX_LINE_HEIGHT - 1.0,
    "rule": 10.0,
}
_APPENDIX_HANGING_STYLES = {"bullet", "quote", "quote_label"}
SYSTEM_LABEL_FONT_SIZE = 8.5
SYSTEM_LABEL_GAP = 5.0
APPENDIX_CONTENT_TOP = PAGE_H - 100.0
LOGICAL_LINE_WIDTH = 1920.0
COORDINATE_TRANSFORM = "remarkable_1404x1872_logical_line_1920_v2"
COORDINATE_PROFILES = {
    (1404.0, 1872.0): {
        "name": COORDINATE_TRANSFORM,
        "logical_width": LOGICAL_LINE_WIDTH,
    }
}
ALLOWED_ACTIONS = {
    "close_ticket": {"ticket_id", "note"},
    "approve_queue_row": {"item_id"},
    "ack_item": {"item_id"},
    "defer_item": {"item_id", "days"},
    "run_job": {"job_id"},
}


class TodayPageError(ValueError):
    """A safe, user-readable validation failure."""


class QueueMovedError(TodayPageError):
    """The review queue changed underneath a compare-and-swap.

    Carries both hashes so a caller can say exactly what it expected to find.
    """

    def __init__(self, expected: str, found: str) -> None:
        super().__init__(
            f"the review queue moved underneath this write "
            f"(expected {expected[:12]}, found {found[:12]})"
        )
        self.expected = expected
        self.found = found


class NoInkReadError(TodayPageError):
    """The archive's page order or ink geometry could not be resolved at all.

    mc-1jw9: distinct from a generic input/config error so the CLI can exit a
    distinct code (3) and alert with fallback-to-manual-read guidance.
    """


# mc-1jw9: every non-success process-archive outcome gets its own exit code so
# a failure is never silent and "direct run" vs "under the watcher" behave
# identically (both propagate the same code from the same source).
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_NO_INK_READ = 3
EXIT_NEEDS_REVIEW = 4
EXIT_CRASH = 5
EXIT_NO_CARDS = 6


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def manifest_digest(manifest: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in manifest.items()
        if key not in {"artifact_hashes", "manifest_hash"}
    }
    return sha256_bytes(canonical_json(stable).encode())


def _validate_v3_pages(manifest: dict[str, Any]) -> None:
    """Prove the v3 page plan: cover, then each card followed by its reading.

    A v3 document interleaves answerable pages and reading pages, so the old
    "every appendix page sits after every answer page" rule does not apply.
    What must still hold is that every page is accounted for exactly once and
    that every answer box sits on a page a card actually occupies - otherwise
    ink could be matched against a page the manifest never claimed.
    """
    page_count = manifest["page_count"]
    cover_pages = manifest.get("cover_pages") or []
    if not cover_pages or cover_pages[0] != 1:
        raise TodayPageError("The document must open with the cover page.")
    card_pages: list[int] = []
    for card in manifest["cards"]:
        numbers = card.get("page_numbers")
        if not isinstance(numbers, list) or not numbers:
            raise TodayPageError("Manifest card pages are missing.")
        card_pages.extend(numbers)
        # "read" must be stated, one way or the other. A current row always
        # names its reading (the parser holds it otherwise); a card rebuilt
        # from a legacy row states null, and says so rather than leaving the
        # field out and letting a reader assume.
        if "read" not in card:
            raise TodayPageError("Manifest card does not say what it is about.")
        read = card["read"]
        if read is not None and (
            not isinstance(read, dict) or not read.get("path")
        ):
            raise TodayPageError("Manifest card is missing its reading document.")
        if not card.get("question_set_hash") or not card.get("question_keys"):
            raise TodayPageError("Manifest card is missing its question keys.")
    action_pages = [
        fragment["page"] for fragment in manifest.get("action_fragments") or []
    ]
    read_pages: list[int] = []
    for appendix in manifest["appendices"]:
        read_pages.extend(appendix.get("page_numbers", []))
    every = list(cover_pages) + action_pages + card_pages + read_pages
    if sorted(every) != list(range(1, page_count + 1)):
        raise TodayPageError(
            "Manifest pages do not account for the document exactly once."
        )
    answerable = set(action_pages) | set(card_pages)
    if manifest.get("answerable_page_count") != len(answerable):
        raise TodayPageError("Manifest answerable page count is invalid.")
    for entry in manifest["entries"]:
        if (
            not isinstance(entry, dict)
            or isinstance(entry.get("page"), bool)
            or not isinstance(entry.get("page"), int)
            or entry["page"] not in answerable
        ):
            raise TodayPageError("Manifest entry page is invalid.")
        if entry.get("kind") != "decision":
            continue
        answer_type = entry.get("answer_type")
        if answer_type not in ("yes_no", "choice", "write_in"):
            raise TodayPageError("Manifest entry answer type is invalid.")
        if not entry.get("question_key"):
            raise TodayPageError("Manifest entry question key is missing.")
        choices = [entry_box["choice"] for entry_box in entry["boxes"]]
        if "DEFER" not in choices or "DISCARD" not in choices:
            raise TodayPageError(
                "Every question must carry a Defer box and a Discard box."
            )
        expected = {
            "yes_no": ["YES", "NO"],
            "write_in": [],
        }.get(answer_type)
        if expected is None:
            expected = list(CHOICE_LETTERS[: len(entry.get("options") or [])])
            if not 2 <= len(expected) <= MAX_CHOICE_OPTIONS:
                raise TodayPageError("A lettered choice needs 2 to 5 options.")
        if choices != expected + ["DEFER", "DISCARD"]:
            raise TodayPageError(
                "Manifest answer boxes do not match the question's answer form."
            )
        if answer_type == "yes_no":
            if entry.get("write_in") is not None:
                raise TodayPageError("A yes or no question has no write-in line.")
        elif not isinstance(entry.get("write_in"), dict):
            raise TodayPageError(
                "A lettered choice or write-in question needs a write-in line."
            )


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise TodayPageError("Manifest schema version is not supported.")
    # mc-vjz5: an allowlist, not equality against the current LAYOUT_VERSION,
    # so resolve() can still replay ink against manifests written under an
    # older ink-compatible layout (see ANSWER_COMPATIBLE_LAYOUT_VERSIONS).
    # Without this, every archived manifest older than the latest bump fails
    # here and re-resolution silently falls back to the stored resolution
    # instead of replaying the ink through the current resolver.
    if manifest.get("layout_version") not in SUPPORTED_LAYOUT_VERSIONS:
        raise TodayPageError("Manifest layout version is not supported.")
    if not isinstance(manifest.get("page_id"), str) or not manifest["page_id"]:
        raise TodayPageError("Manifest page id is missing.")
    if not isinstance(manifest.get("entries"), list):
        raise TodayPageError("Manifest entries are missing.")
    if not isinstance(manifest.get("cards"), list):
        raise TodayPageError("Manifest cards are missing.")
    if not isinstance(manifest.get("appendices"), list):
        raise TodayPageError("Manifest appendices are missing.")
    page_count = manifest.get("page_count")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise TodayPageError("Manifest page count is missing.")
    answerable_page_count = manifest.get("answerable_page_count")
    # A v3 document with nothing to answer is still a real document: it is the
    # cover page saying so, and page 1 names every row that was held. Schema 1
    # had no cover page, so a document with no answerable page was a bug there.
    fewest_answerable = 0 if manifest["schema_version"] >= 2 else 1
    if (
        isinstance(answerable_page_count, bool)
        or not isinstance(answerable_page_count, int)
        or not fewest_answerable <= answerable_page_count <= page_count
    ):
        raise TodayPageError("Manifest answerable page count is invalid.")
    if manifest["schema_version"] >= 2:
        _validate_v3_pages(manifest)
    else:
        for entry in manifest["entries"]:
            if (
                not isinstance(entry, dict)
                or isinstance(entry.get("page"), bool)
                or not isinstance(entry.get("page"), int)
                or not 1 <= entry["page"] <= answerable_page_count
            ):
                raise TodayPageError("Manifest entry page is invalid.")
        appendix_pages = [
            page
            for appendix in manifest["appendices"]
            if isinstance(appendix, dict)
            for page in appendix.get("page_numbers", [])
        ]
        if sorted(appendix_pages) != list(
            range(answerable_page_count + 1, page_count + 1)
        ):
            raise TodayPageError("Manifest appendix pages are incomplete or overlap.")
    # An id is a nonblank string, checked before uniqueness and membership: an
    # empty string or a number used consistently by an entry and its card
    # would otherwise pass both (the cross-reviewer's review of the phone-question delivery
    # check, mc-0mtb0, 2026-11-09). Every archived manifest's ids are
    # nonblank strings (12-character hex now, "decision-001a" style before).
    def _id_ok(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    if not all(_id_ok(entry.get("entry_id")) for entry in manifest["entries"]):
        raise TodayPageError("Manifest entry ids are missing, blank or not strings.")
    entry_ids = {entry.get("entry_id") for entry in manifest["entries"]}
    if None in entry_ids or len(entry_ids) != len(manifest["entries"]):
        raise TodayPageError("Manifest entry ids are missing or repeated.")
    for card in manifest["cards"]:
        if (
            not isinstance(card, dict)
            or not isinstance(card.get("decision_entry_ids"), list)
            or not all(_id_ok(e) for e in card["decision_entry_ids"])
            or not card["decision_entry_ids"]
            or not set(card["decision_entry_ids"]).issubset(entry_ids)
            or not 1 <= card.get("page", 0) <= page_count
        ):
            raise TodayPageError("Manifest card is invalid.")
    if manifest.get("manifest_hash") != manifest_digest(manifest):
        raise TodayPageError("Manifest integrity check failed. No action was taken.")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# The review queue is shared. Every writer takes the same lock (mc-63x2).
#
# A compare-and-swap alone cannot protect a file: the comparison and the
# replacement are two steps, and a writer that lands between them is compared
# against nothing and silently overwritten. The lock lives here, beside the
# queue's other writers, so consume_decisions, the 06:00 generator and
# execute() all serialise against each other rather than each guarding its own
# door.
# ---------------------------------------------------------------------------

QUEUE_LOCK_TIMEOUT_SECONDS = 30.0

# (resolved queue path, thread id) -> nesting depth. Re-entering the lock on
# the same thread is a no-op; a second THREAD still blocks on the real flock,
# because flock is held per open file description, not per process.
_HELD_QUEUE_LOCKS: dict[tuple[str, int], int] = {}
_QUEUE_LOCK_REGISTRY = threading.Lock()


def queue_lock_path(queue_path: Path) -> Path:
    return Path(queue_path).with_name(Path(queue_path).name + ".lock")


@contextlib.contextmanager
def queue_lock(
    queue_path: Path, timeout: float = QUEUE_LOCK_TIMEOUT_SECONDS
) -> Iterable[Path]:
    """Exclusive advisory lock for one review queue file.

    Every writer of the queue must hold this for its whole read-modify-write,
    not just for the write: reading the file, deciding what to change and
    replacing it is one operation, and splitting it is exactly how a
    concurrent writer's work disappears.
    """
    path = Path(queue_path)
    lock_path = queue_lock_path(path)
    key = (str(path.resolve()), threading.get_ident())
    with _QUEUE_LOCK_REGISTRY:
        depth = _HELD_QUEUE_LOCKS.get(key, 0)
        if depth:
            _HELD_QUEUE_LOCKS[key] = depth + 1
    if depth:
        try:
            yield lock_path
        finally:
            with _QUEUE_LOCK_REGISTRY:
                remaining = _HELD_QUEUE_LOCKS.get(key, 1) - 1
                if remaining > 0:
                    _HELD_QUEUE_LOCKS[key] = remaining
                else:
                    _HELD_QUEUE_LOCKS.pop(key, None)
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TodayPageError(
                        f"another writer has held {lock_path} for more than "
                        f"{timeout:.0f} seconds; no queue change was made"
                    ) from exc
                time.sleep(0.05)
        handle.write(f"{os.getpid()} {dt.datetime.now().isoformat()}\n")
        handle.flush()
        with _QUEUE_LOCK_REGISTRY:
            _HELD_QUEUE_LOCKS[key] = 1
        try:
            yield lock_path
        finally:
            with _QUEUE_LOCK_REGISTRY:
                _HELD_QUEUE_LOCKS.pop(key, None)
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def replace_queue_verified(path: Path, text: str, expected_sha: str) -> str:
    """Replace the queue only if it still hashes to expected_sha.

    The verification is the last thing that happens before os.replace, inside
    the lock, with nothing between them: no other call, no other write. A
    writer that does not hold the lock is the only thing this cannot see, which
    is why every writer takes it.
    """
    path = Path(path)
    with queue_lock(path):
        data = text.encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            live = path.read_bytes() if path.is_file() else b""
            live_sha = sha256_bytes(live)
            if live_sha != expected_sha:
                raise QueueMovedError(expected_sha, live_sha)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
    return sha256_bytes(data)


def escape_table_cell(text: str) -> str:
    """Make arbitrary text safe to sit in one markdown table cell.

    _split_row is this repo's only row-detection authority and it splits on a
    raw pipe, so an Ask carrying one pipe turns a three-column Closed row into
    four cells and the disposition is read as the wrong column. The numeric
    entity renders as a pipe everywhere markdown is rendered and is invisible
    to any splitter.
    """
    return text.replace("|", "&#124;")


def load_verification_report(path: Path) -> dict[str, Any]:
    report = _read_json(path)
    verified_at = report.get("verified_at")
    if not isinstance(verified_at, str):
        raise TodayPageError("The verification report has no verified_at timestamp.")
    try:
        parsed = dt.datetime.fromisoformat(verified_at)
    except ValueError as exc:
        raise TodayPageError(
            "The verification report timestamp is not valid ISO 8601."
        ) from exc
    if parsed.tzinfo is None:
        raise TodayPageError("The verification timestamp must include a time zone.")
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise TodayPageError("The verification report has no row results.")
    verified_titles = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("title"), str)
            or not isinstance(row.get("ok"), bool)
        ):
            raise TodayPageError("A verification report row is malformed.")
        if row["ok"]:
            verified_titles.append(clean_markdown(row["title"]))
    hour = parsed.strftime("%I").lstrip("0") or "0"
    human = (
        f"{parsed.strftime('%B')} {parsed.day} at "
        f"{hour}:{parsed.strftime('%M %p')}"
    )
    # mc-iacbw section 5: a held row is named on page 1 of the document, in
    # plain English, with its reason. Nothing is withheld silently, so the
    # reasons travel with the report rather than staying in a log file.
    held = []
    for row in rows:
        if row["ok"]:
            continue
        reasons = [
            str(reason)
            for reason in (row.get("reasons") or [])
            if str(reason).strip()
        ]
        held.append(
            {
                "title": clean_markdown(row["title"]),
                "reason": _plain_hold_reason(reasons),
            }
        )
    return {
        "verified_at": verified_at,
        "verified_at_human": human,
        "verified_titles": verified_titles,
        "held": held,
    }


_PLAIN_HOLD_REASONS = (
    (
        "does not say how it is answered",
        "the card does not say how one of its questions is answered, so it "
        "would have printed the wrong boxes",
    ),
    (
        "document to read",
        "the document this card is about could not be read, so the card would "
        "have arrived without it",
    ),
    (
        "which is closed",
        "it points at a ticket that has since been closed, so the question may "
        "already be settled",
    ),
    (
        "unknown to the tracker",
        "it points at a ticket the tracker does not have",
    ),
    (
        "referenced file missing",
        "a file it points at is missing",
    ),
)


def _plain_hold_reason(reasons: list[str]) -> str:
    """One plain-English sentence for why a row was held (contract section 5)."""
    if not reasons:
        return "it did not pass the morning check"
    for needle, plain in _PLAIN_HOLD_REASONS:
        for reason in reasons:
            if needle in reason:
                return plain
    first = reasons[0].strip()
    first = first[0].lower() + first[1:] if first else first
    return first.rstrip(".")


def clean_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    value = clean_markdown(text)
    if not value or value[-1] not in ".!?":
        raise TodayPageError("Decision context must end with terminal punctuation.")
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    ]
    if any(sentence[-1] not in ".!?" for sentence in sentences):
        raise TodayPageError("Every decision context sentence needs punctuation.")
    return sentences


def _question_from_title(title: str) -> str:
    question = re.sub(r"\s+\([^()]*\)\s*$", "", clean_markdown(title)).strip()
    if not question:
        raise TodayPageError("Each decision needs a plain-English question.")
    if question[-1] not in ".!?":
        question += "?"
    elif question[-1] == ".":
        question = question[:-1] + "?"
    return question


def _human_date(value: str) -> str:
    parsed = dt.date.fromisoformat(value)
    return f"{parsed.strftime('%B')} {parsed.day}"


def _date_line(age: str) -> str:
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})(?:;\s*rev\s+(\d{4}-\d{2}-\d{2}))?",
        clean_markdown(age),
    )
    if not match:
        raise TodayPageError(
            "Decision age must be YYYY-MM-DD or YYYY-MM-DD; rev YYYY-MM-DD."
        )
    line = f"Raised {_human_date(match.group(1))}."
    if match.group(2):
        line += f" Revised {_human_date(match.group(2))}."
    return line


# mc-bbefy: every Decide question renders with a YES box and a NO box, so
# every Decide question must be answerable yes or no. The reader 2026-09-01: two
# questions rendered with YES/NO boxes that could not be answered that way
# ("If not, which buckets do you want to rule on now by chat?" and "If yes,
# limit it to things handed to you, or include agent-to-agent messages that
# change a settled direction?"). This is a gate, not a prose rule.
_INTERROGATIVE_WORDS = (
    "which",
    "what",
    "how",
    "where",
    "who",
    "whom",
    "when",
    "why",
)
_INTERROGATIVE_CLAUSE = re.compile(
    r"^(" + "|".join(_INTERROGATIVE_WORDS) + r")\b", re.IGNORECASE
)
# "which" doubles as a relative pronoun ("...a different circuit first, which
# is free but proves less?"). Only treat a "which" clause as an open question
# when it heads a noun phrase (which buckets, which files); a following
# copula/aux verb marks a relative clause describing the previous noun, not
# an interrogative one, and must not be rejected.
_WHICH_RELATIVE_FOLLOWER = re.compile(
    r"^which\s+(is|are|was|were|do|does|did|will|would|can|could|should|has|had|means)\b",
    re.IGNORECASE,
)
_EITHER_START = re.compile(r"^either\b", re.IGNORECASE)
_EITHER_OR_COMMA = re.compile(r",\s*or\s", re.IGNORECASE)
# A bare "or" joining two verb phrases in one question ("...acceptable, or
# should it be closed..." without the comma). Excludes a question that
# simply *starts* with "Or" (e.g. "Or will you paste the line in yourself
# for me to verify?"), which is a standalone follow-on sub-question in this
# system's numbered lists and is answerable yes/no on its own.
_EITHER_OR_AUX = re.compile(
    r"\bor\s+(do|does|did|will|would|should|shall|can|could|is|are|was|were|must)\s+"
    r"(you|we|it|they|i)\b",
    re.IGNORECASE,
)
_VERSUS = re.compile(r"\bversus\b|\bvs\.?\b", re.IGNORECASE)

# The stable per-question key from the row, "(n:key)". State (answered,
# deferred, discarded) is keyed on it, so it survives a wording edit; a
# genuinely different question gets a new key (contract section 1).
_QUESTION_KEY = re.compile(r"[a-z0-9_]+")
_FORM_TAG = re.compile(r"^\[([^\]]*)\]\s*(.*)$", re.DOTALL)
_CHOICE_SPLIT = re.compile(r",\s*(?=[A-Za-z]\s*=)")
_CHOICE_OPTION = re.compile(r"^([A-Za-z])\s*=\s*(.+)$", re.DOTALL)


_QUESTION_RECOMMENDATION = re.compile(r"\s*Recommended:\s*(.+?)\s*$", re.DOTALL)

# A recommendation is exactly one sentence: one period, at the end, none
# inside. A period that sits inside a quoted or parenthesized token is
# masked out first so it is not counted against that rule, UNLESS the period
# itself reads as a sentence boundary (a period followed by a space and a
# capital letter) - that shape is what a second sentence hidden inside a
# quote looks like, and hiding it let "Recommended: A because "First.
# Second." is the rationale." pass with one hidden sentence break (the cross-reviewer's
# re-check, 2026-09-14). An abbreviation ("e.g.") or a version number
# ("1.6.6") never has a space-then-capital after its internal periods, so
# those keep getting masked and keep passing.
_QUOTE_OR_PAREN_SPAN = re.compile(r'"[^"]*"|\([^()]*\)')
_SENTENCE_END_PERIOD = re.compile(r"\.(?=\s+[A-Z])")

# The special answer that is valid on a choice or yes/no question when
# neither option matters (the cross-reviewer's review, 2026-09-14, item 2 of the fix).
_NO_PREFERENCE_ANSWER = "no preference, both are workable"

_RECOMMENDATION_BECAUSE = re.compile(r"\bbecause\b", re.IGNORECASE)
# "because" must introduce an actual reason, not sit bare at the end of the
# clause: "Recommended: A." with no reason at all used to pass with no
# "because" check at all (the cross-reviewer's re-check, 2026-09-14, item 1). The one
# exemption is the exact no-preference form with no reason attached.
_RECOMMENDATION_BECAUSE_REASON = re.compile(r"\bbecause\b\s+\S", re.IGNORECASE)

MAX_WRITE_IN_RECOMMENDATION_LENGTH = 80


def _split_question_recommendation(
    text: str, number: int
) -> tuple[str, str | None]:
    """Split one question's own "Recommended: ..." clause off its text.

    The reader, 2026-09-14: "the recommendation only covers the first decision ...
    It would be better to have a recommendation associated with each question,
    presented with each question." A card-level Recommendation section says
    nothing about question 2, so every question may carry its own clause,
    written immediately after the question text, and the renderer prints it
    directly under that question.
    """
    if "Recommended:" not in text:
        return text.strip(), None
    if text.count("Recommended:") > 1:
        raise TodayPageError(
            f"Decide question {number} carries more than one "
            "\"Recommended:\" clause; each question gets exactly one."
        )
    match = _QUESTION_RECOMMENDATION.search(text)
    if not match:
        raise TodayPageError(
            f"Decide question {number} has a \"Recommended:\" clause that is "
            "not at the end of the question text."
        )
    recommendation = re.sub(r"\s+", " ", match.group(1)).strip()
    if not recommendation:
        raise TodayPageError(
            f"Decide question {number} has an empty \"Recommended:\" clause."
        )
    bare_answer = recommendation.rstrip(".").strip().lower()
    if bare_answer != _NO_PREFERENCE_ANSWER and not _RECOMMENDATION_BECAUSE_REASON.search(
        recommendation
    ):
        raise TodayPageError(
            f"Decide question {number} has a recommendation with no reason. "
            "Write it as \"Recommended: <answer> because <reason>.\" or, "
            "when neither option matters, \"Recommended: no preference, "
            "both are workable.\""
        )

    def _mask_span(found: "re.Match[str]") -> str:
        # Inside one quoted or parenthesized span: a period that reads as a
        # sentence boundary (followed by a space and a capital letter) stays
        # a period, so it is still counted below. Every other period in the
        # span (an abbreviation, a version number, a trailing period right
        # against the closing quote or paren) is masked out as before.
        span = found.group(0)
        return "".join(
            character
            if character != "." or _SENTENCE_END_PERIOD.match(span, position)
            else "\x00"
            for position, character in enumerate(span)
        )

    masked = _QUOTE_OR_PAREN_SPAN.sub(_mask_span, recommendation)
    if not masked.endswith("."):
        raise TodayPageError(
            f"Decide question {number} has a recommendation that is not a "
            "complete sentence."
        )
    if masked.count(".") != 1:
        raise TodayPageError(
            f"Decide question {number} has a recommendation with more than "
            "one sentence; write it as a single sentence ending in one "
            "period."
        )
    return text[: match.start()].strip(), recommendation


def _recommended_answer(recommendation: str) -> str:
    """The answer clause of a "Recommended: <answer> because <reason>." line.

    Cuts at the first "because"; a recommendation with no "because" (for
    example a bare "Recommended: C") is taken whole, minus its terminal
    period, so a malformed clause still gets checked against the question's
    form rather than silently passing.
    """
    match = _RECOMMENDATION_BECAUSE.search(recommendation)
    if match:
        return recommendation[: match.start()].strip()
    return recommendation.rstrip(".").strip()


def _validate_question_recommendation(
    recommendation: str | None,
    answer_type: str,
    options: list[dict[str, str]],
    number: int,
) -> None:
    """Hold a row whose recommended answer does not fit the question's form.

    The cross-reviewer's review, 2026-09-14: the per-question recommendation gate
    checked that a "Recommended: X because ..." clause was present, but
    never checked X against the form the question actually offers.
    "Recommended: C" passed on an A/B choice question, and a yes/no question
    could be "recommended" a lettered answer. This closes that gap.
    """
    if recommendation is None:
        return
    answer = _recommended_answer(recommendation)
    lowered = answer.lower()
    if answer_type == "choice":
        letters = [option["letter"] for option in options]
        if lowered == _NO_PREFERENCE_ANSWER:
            return
        if answer.strip().upper() not in letters:
            joined = (
                letters[0]
                if len(letters) <= 1
                else ", ".join(letters[:-1]) + " and " + letters[-1]
            )
            raise TodayPageError(
                f"question {number} recommends {answer.strip()} but the "
                f"choices are {joined}"
            )
        return
    if answer_type == "yes_no":
        if lowered in ("yes", "no", _NO_PREFERENCE_ANSWER):
            return
        raise TodayPageError(
            f"question {number} recommends {answer.strip()} but this "
            "question only takes yes or no"
        )
    if answer_type == "write_in":
        if len(answer) > MAX_WRITE_IN_RECOMMENDATION_LENGTH:
            raise TodayPageError(
                f"question {number}'s recommended answer is longer than "
                f"{MAX_WRITE_IN_RECOMMENDATION_LENGTH} characters"
            )
        return


def _parse_form_tag(
    raw: str,
    number: int,
    require_form: bool = True,
) -> dict[str, Any]:
    """Read one question's answer-form tag and return the question with it.

    The reader answers each question in the form the question needs (interview
    answer 1, 2026-09-01). The card writer states that form in the row, in
    square brackets before the question: [yes/no], [write-in], or
    [choice: A = ..., B = ...]. Guessing the form from the wording is exactly
    what put Y and N boxes under two open questions on the 2026-09-01 sheet,
    so a missing or malformed tag is a contract failure that holds the row.
    """
    match = _FORM_TAG.match(raw.strip())
    if not match:
        if require_form:
            raise TodayPageError(
                f"Decide question {number} does not say how it is answered. "
                "Put [yes/no], [choice: A = ..., B = ...] or [write-in] in "
                "front of the question text."
            )
        # Legacy shape: every v2 question was printed with YES and NO boxes,
        # so that is what an untagged question meant.
        question, recommendation = _split_question_recommendation(
            raw.strip(), number
        )
        if not question.endswith("?"):
            raise TodayPageError(
                f"Decide question {number} must end with a question mark."
            )
        _validate_question_recommendation(recommendation, "yes_no", [], number)
        return {
            "answer_type": "yes_no",
            "question": question,
            "options": [],
            "recommendation": recommendation,
        }
    body = match.group(1).strip()
    question, recommendation = _split_question_recommendation(
        match.group(2).strip(), number
    )
    if not question:
        raise TodayPageError(
            f"Decide question {number} is only an answer form with no question."
        )
    if not question.endswith("?"):
        raise TodayPageError(
            f"Decide question {number} must end with a question mark."
        )
    lowered = body.lower()
    if lowered in ("yes/no", "yes / no"):
        _validate_question_recommendation(recommendation, "yes_no", [], number)
        return {
            "answer_type": "yes_no",
            "question": question,
            "options": [],
            "recommendation": recommendation,
        }
    if lowered in ("write-in", "write in"):
        _validate_question_recommendation(recommendation, "write_in", [], number)
        return {
            "answer_type": "write_in",
            "question": question,
            "options": [],
            "recommendation": recommendation,
        }
    if not lowered.startswith("choice:"):
        raise TodayPageError(
            f"Decide question {number} has an answer form this document does "
            f"not print ({body!r}). Use [yes/no], [choice: A = ..., B = ...] "
            "or [write-in]."
        )
    options: list[dict[str, str]] = []
    for part in _CHOICE_SPLIT.split(body[len("choice:"):].strip()):
        option = _CHOICE_OPTION.match(part.strip())
        if not option:
            raise TodayPageError(
                f"Decide question {number} has a choice that is not written "
                f"as a letter, an equals sign and its text ({part.strip()!r})."
            )
        options.append(
            {"letter": option.group(1).upper(), "text": option.group(2).strip()}
        )
    if not 2 <= len(options) <= MAX_CHOICE_OPTIONS:
        raise TodayPageError(
            f"Decide question {number} offers {len(options)} lettered choices; "
            f"a choice question needs between 2 and {MAX_CHOICE_OPTIONS}."
        )
    expected = list(CHOICE_LETTERS[: len(options)])
    if [option["letter"] for option in options] != expected:
        raise TodayPageError(
            f"Decide question {number} must letter its choices "
            f"{', '.join(expected)} in order."
        )
    if any(not option["text"] for option in options):
        raise TodayPageError(
            f"Decide question {number} has a lettered choice with no text."
        )
    _validate_question_recommendation(recommendation, "choice", options, number)
    return {
        "answer_type": "choice",
        "question": question,
        "options": options,
        "recommendation": recommendation,
    }


def _numbered_decisions(
    text: str,
    require_keys: bool = True,
) -> list[dict[str, Any]]:
    """Parse the authoritative numbered list after the Decide marker.

    require_keys=True is the current contract: every question numbered
    "(n:key)" with an answer form. require_keys=False reads the LEGACY row
    shape - bare "(n)" and no form tag - which archived queue sections,
    archived card asks and the consumer's legacy path still contain and must
    keep reading. A legacy question is given the positional key "qn" so the
    rest of the pipeline has something stable to work with within that one
    document; it is not, and must not be treated as, a durable identity.
    """
    # clean_markdown strips underscores (markdown emphasis), which would
    # silently turn the question key "go_ahead" into "goahead" and could
    # collide two different keys into one. Keys are protected across the
    # clean, then restored, so the row's key is the key the manifest carries.
    guarded = re.sub(
        r"\((\d+):([A-Za-z0-9_]*)\)",
        lambda found: f"({found.group(1)}:{found.group(2).replace('_', chr(1))})",
        text,
    )
    value = clean_markdown(guarded).replace(chr(1), "_")
    matches = list(re.finditer(r"(?<!\S)\((\d+)(?::([A-Za-z0-9_]*))?\)\s*", value))
    if not matches or matches[0].start() != 0:
        raise TodayPageError("The Decide section must start with (1:key).")
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(matches) + 1)):
        raise TodayPageError("Decide question numbers must be contiguous from 1.")
    if len(matches) > MAX_SUBDECISIONS:
        raise TodayPageError(
            f"A decision card can contain no more than {MAX_SUBDECISIONS} questions."
        )
    questions: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        question = value[match.end() : end].strip()
        if not question:
            raise TodayPageError(
                f"Decide question {index + 1} has no text."
            )
        key = match.group(2)
        if not key:
            if require_keys:
                raise TodayPageError(
                    f"Decide question {index + 1} has no question key. Number "
                    f"it ({index + 1}:short_key) so the answer stays attached "
                    "to the question when the wording changes."
                )
            key = f"q{index + 1}"
        if not _QUESTION_KEY.fullmatch(key):
            raise TodayPageError(
                f"Decide question {index + 1} has a question key that is not "
                f"lowercase letters, digits and underscores ({key!r})."
            )
        if key in keys:
            raise TodayPageError(
                f"Decide question {index + 1} repeats the question key {key!r}; "
                "keys must be unique within a row."
            )
        keys.add(key)
        parsed = _parse_form_tag(question, index + 1, require_form=require_keys)
        parsed["question_key"] = key
        questions.append(parsed)
    return questions


_READ_MARKER = re.compile(r"Read:\s*([^\s|]+?)\s*\.?\s*$")
_ROW_MARKER = re.compile(r"Row:\s*([^\s|]+?)\s*\.?\s*$")


_ANSWERED_MARKER = re.compile(r"Answered:\s*(.+?)\s*$")
_ANSWERED_ENTRY = re.compile(
    r"([A-Za-z0-9_]+)\s+on\s+(\d{4}-\d{2}-\d{2})\s+by\s+"
    r"(pen|session|imessage|email)",
    re.IGNORECASE,
)
# The reader's worst-named failure is being re-asked something he already answered.
# The durable record covered pen marks (the "## Pen answers" sections and the
# archived resolutions) and the question-state file, but NOT an answer he gave
# out loud in a session or by iMessage: those were written into the row as
# prose, which nothing machine-readable ever saw. A row now declares such an
# answer with "Answered: <question key> on <date> by <pen|session|imessage|
# email>", and a declared question is never printed again (mc-9jd4t).


def _split_answered(ask: str) -> tuple[str, dict[str, dict[str, str]]]:
    """Split the trailing "Answered: ..." declarations off one Ask."""
    count = len(re.findall(r"\bAnswered:", ask))
    if count == 0:
        return ask, {}
    if count > 1:
        raise TodayPageError(
            "An Ask can carry only one \"Answered:\" clause; list every "
            "recorded answer in it, separated by semicolons."
        )
    match = _ANSWERED_MARKER.search(ask)
    if not match:
        raise TodayPageError(
            "Recorded answers must be declared just before \"GH-checked:\" or "
            "\"Row:\", as \"Answered: <question key> on <YYYY-MM-DD> by "
            "<pen|session|imessage|email>\"."
        )
    answered: dict[str, dict[str, str]] = {}
    for part in [p.strip() for p in match.group(1).split(";") if p.strip()]:
        entry = _ANSWERED_ENTRY.fullmatch(part.rstrip("."))
        if not entry:
            raise TodayPageError(
                "Each recorded answer must read \"<question key> on "
                "<YYYY-MM-DD> by <pen|session|imessage|email>\". This one "
                f"does not: {part!r}"
            )
        answered[entry.group(1)] = {
            "answered_on": entry.group(2),
            "channel": entry.group(3).lower(),
        }
    return ask[: match.start()].rstrip(), answered


def _refuse_recorded_answers(
    forms: list[dict[str, Any]], answered: dict[str, dict[str, str]]
) -> None:
    """Refuse a card that still prints a question already answered."""
    if not answered:
        return
    printed = {form["question_key"] for form in forms}
    clash = sorted(printed & set(answered))
    if clash:
        detail = "; ".join(
            f"{key} was answered on {answered[key]['answered_on']} by "
            f"{answered[key]['channel']}"
            for key in clash
        )
        raise TodayPageError(
            "This row still prints a question that already carries a "
            f"recorded answer ({detail}). Remove the answered question and "
            "rewrite the Ask around what is still open, or move the row to "
            "Closed."
        )


_GH_MARKER = re.compile(r"GH-checked:\s*(.+?)\s*$")
_GH_ENTRY = re.compile(
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\s+"
    r"(OPEN|CLOSED|MERGED|DRAFT)\s+"
    r"updated\s+(\S+?),?\s+"
    r"checked\s+(\d{4}-\d{2}-\d{2})"
)
# A card can also be about a pull request that does not exist yet, which is
# still a live GitHub fact and still goes stale overnight: somebody may open
# one. "NONE" records that the repository was searched on the day and carried
# no such pull request or issue.
_GH_NONE = re.compile(
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+NONE\s+"
    r"checked\s+(\d{4}-\d{2}-\d{2})"
)
_FACTS_MARKER = re.compile(r"(?:Facts|Freshness):\s*(.+?)\s*$")
_LEGACY_FACTS_CHECKED = re.compile(r"\s+checked\s+(\d{4}-\d{2}-\d{2})\s*$")
_TICKET_MARKER = re.compile(r"Tickets?:\s*(.+?)\s*$")
_FACT_TRIGGER = re.compile(
    r"\b(?:today|tonight|overnight|still|while you travel|while you are traveling|"
    r"after your trip|last week|not live|not on your tablet|none is merged|"
    r"nothing is installed|nothing runs on a schedule yet)\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d+\s+(?:rows?|cards?|items?|errors?|accidents?|losses?|samples?)\b",
    re.IGNORECASE,
)

# A card that talks about somebody else's pull request or issue is talking
# about state that lives on GitHub, not in this repository, and nothing in the
# render path used to look at it. On 2026-09-12 the reader edited and published the
# build-tool pull request texts himself; the next morning the document still asked
# him to approve them, because the row was written from the repo and the
# generator had no way to know. A row that mentions a pull request or an issue
# now has to carry the live state and last-updated time from the day it
# renders, or it does not render (mc-9jd4t).
_GH_MENTION = re.compile(
    r"github\.com/|\bpull requests?\b|\bissues?\s*#\d|"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+",
    re.IGNORECASE,
)


# No answer card may tell the reader to read something before he can answer it. A
# card that needs its appendix read first is a card that failed to carry its
# own context, which is the whole complaint behind this rebuild. The appendix
# is background (the cross-reviewer's review, 2026-09-13, mc-9jd4t).
_MANDATORY_READ = re.compile(
    r"read\s+(?:the\s+)?(?:pages?|appendix|reading|document|attachment)\b"
    r"[^.]{0,60}?\b(?:then|before|first)\b"
    r"|\b(?:then|before)\s+answer(?:ing)?\b"
    r"|\bmust\s+read\b"
    r"|\bread\s+this\s+first\b",
    re.IGNORECASE,
)


def _appendix_reference_note(start: int, end: int) -> str:
    """The one-line pointer to a card's background reading."""
    note = (
        f"Background, optional: page {start}."
        if start == end
        else f"Background, optional: pages {start} to {end}."
    )
    _refuse_mandatory_read_wording(note, "the appendix reference")
    return note


def _refuse_mandatory_read_wording(text: str, where: str) -> None:
    """Refuse text that makes reading the appendix a precondition."""
    found = _MANDATORY_READ.search(clean_markdown(text))
    if found:
        raise TodayPageError(
            f"No card may require reading before answering, and {where} does: "
            f"{found.group(0)!r}. Put what he needs on the card and refer to "
            "the reading as \"Background, optional: pages X to Y.\""
        )


# mc-9jd4t: the cross-reviewer failed a card on 2026-09-13 that printed "25.2 GiB free
# ... above the 25 GiB floor". By the time he read it, df said 24.97, so the
# card's claim had flipped while it sat under review. Free space moves by the
# minute. A card may still state it, but only as a reading taken at a named
# time, and never as a comparison against a threshold, because the comparison
# is the part that goes stale.
_FREE_SPACE_FIGURE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:GiB|GB|TiB|TB|MiB|MB)\s+free\b", re.I
)
_FREE_SPACE_TIME = re.compile(
    r"\b(?:"
    r"\d{1,2}:\d{2}"                     # 16:58
    r"|as of\b"
    r"|when this was written\b"
    r"|measured\b"
    r"|read at\b"
    r"|at \d{1,2}\s*(?:am|pm)\b"
    r")", re.I
)
_FREE_SPACE_COMPARISON = re.compile(
    r"\b(?:just )?(?:above|below|over|under|clear of|short of)\s+"
    r"(?:the\s+)?\S*\s*\d+(?:\.\d+)?\s*(?:GiB|GB|TiB|TB|MiB|MB)",
    re.I,
)


def _require_timed_free_space(text: str) -> None:
    """A free-space figure must carry the time it was read, and no verdict.

    Two separate refusals. Without a time, the reader cannot tell whether the
    number is minutes or hours old. With a comparison against a floor, the
    card asserts a relationship that can reverse between rendering and
    reading, which is exactly what happened on 2026-09-13.
    """
    clean = clean_markdown(text)
    figure = _FREE_SPACE_FIGURE.search(clean)
    if not figure:
        return
    if not _FREE_SPACE_TIME.search(clean):
        raise TodayPageError(
            "A card that states free disk space must say when the reading was "
            f"taken, and this one does not: {figure.group(0)!r}. Write it as "
            "\"About N GiB free when this was written (HH:MM today)\"."
        )
    verdict = _FREE_SPACE_COMPARISON.search(clean)
    if verdict:
        raise TodayPageError(
            "A card may not say whether free space is above or below a "
            f"threshold, and this one does: {verdict.group(0)!r}. The "
            "comparison can reverse between rendering and reading. State the "
            "measured figure with its time and the floor as a plain number."
        )


def _split_gh_checks(ask: str) -> tuple[str, list[dict[str, str]]]:
    """Split the trailing "GH-checked: ..." declaration off one Ask."""
    count = len(re.findall(r"\bGH-checked:", ask))
    if count == 0:
        return ask, []
    if count > 1:
        raise TodayPageError(
            "An Ask can carry only one \"GH-checked:\" clause, once, just "
            "before \"Row:\"."
        )
    match = _GH_MARKER.search(ask)
    if not match:
        raise TodayPageError(
            "The GitHub freshness check must come just before \"Row:\", as "
            "\"GH-checked: <owner>/<repo>#<number> <STATE> updated "
            "<timestamp>, checked <YYYY-MM-DD>\"."
        )
    body = match.group(1).rstrip(". ")
    checks = []
    for part in [p.strip() for p in body.split(";") if p.strip()]:
        absent = _GH_NONE.fullmatch(part.rstrip("."))
        if absent:
            checks.append(
                {
                    "repo": absent.group(1),
                    "number": "",
                    "state": "NONE",
                    "updated_at": "",
                    "checked_on": absent.group(2),
                }
            )
            continue
        entry = _GH_ENTRY.fullmatch(part.rstrip("."))
        if not entry:
            raise TodayPageError(
                "Each GitHub freshness check must read \"<owner>/<repo>#"
                "<number> <STATE> updated <timestamp>, checked "
                f"<YYYY-MM-DD>\". This one does not: {part!r}"
            )
        checks.append(
            {
                "repo": entry.group(1),
                "number": entry.group(2),
                "state": entry.group(3),
                "updated_at": entry.group(4),
                "checked_on": entry.group(5),
            }
        )
    return ask[: match.start()].rstrip(), checks


def _split_fact_check(ask: str) -> tuple[str, dict[str, str] | None]:
    """Split trailing "Facts:" / "Freshness:" verification metadata.

    Syntax: Facts: <repo-relative verifier> [args...]
    A legacy trailing "checked YYYY-MM-DD" is accepted and ignored; the
    render-time verifier execution is the freshness proof.
    """
    count = len(re.findall(r"\b(?:Facts|Freshness):", ask))
    if count == 0:
        return ask, None
    if count > 1:
        raise TodayPageError(
            'An Ask can carry only one "Facts:" or "Freshness:" clause, '
            'just before "Row:".'
        )
    match = _FACTS_MARKER.search(ask)
    if not match:
        raise TodayPageError(
            'The Facts freshness check must come just before "Row:", as '
            '"Facts: <repo-relative verifier> [args...]". A legacy trailing '
            '"checked <YYYY-MM-DD>" is accepted but ignored.'
        )
    command = match.group(1).strip()
    legacy_checked = None
    legacy = _LEGACY_FACTS_CHECKED.search(command)
    if legacy:
        legacy_checked = legacy.group(1)
        command = command[: legacy.start()].strip()
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise TodayPageError(f"The Facts verifier command is not valid shell syntax: {exc}.") from exc
    if not parts:
        raise TodayPageError("The Facts freshness check names no verifier command.")
    data = {
        "command": command,
        "parts": parts,
    }
    if legacy_checked:
        data["legacy_checked_on"] = legacy_checked
    return ask[: match.start()].rstrip(), data


def _split_ticket_links(ask: str) -> tuple[str, str | None]:
    """Split a trailing Ticket/Tickets marker off the parser core.

    The original Ask string still goes into the manifest, so card_premise_watch
    can read this marker. Removing it from the core keeps "Ticket:" from being
    printed as part of the Decide prose or per-question recommendation.
    """
    count = len(re.findall(r"\bTickets?:", ask))
    if count == 0:
        return ask, None
    if count > 1:
        raise TodayPageError('An Ask can carry only one trailing "Ticket:" or "Tickets:" clause.')
    match = _TICKET_MARKER.search(ask)
    if not match:
        raise TodayPageError('The Ticket marker must come just before "Facts:" or "Row:".')
    return ask[: match.start()].rstrip(), match.group(1).strip()


def _facts_need_verification(text: str) -> bool:
    return bool(_FACT_TRIGGER.search(clean_markdown(text)))


def _require_fresh_facts(
    fact_text: str,
    facts: dict[str, str] | None,
    render_date: str,
    row_key: str | None,
    read_root: Path,
    queue_path: Path | None = None,
) -> None:
    """Require and run verifier for dated/situational/count claims."""
    needs = _facts_need_verification(fact_text)
    if needs and not facts:
        raise TodayPageError(
            "This card carries dated, situational, or count facts, so it must "
            "carry a render-time Facts check just before Row:, as "
            '"Facts: <repo-relative verifier> [args...]".'
        )
    if not facts:
        return
    parts = list(facts["parts"])
    command = parts[0]
    if command.startswith("/") or command.startswith("~"):
        raise TodayPageError(
            f"The Facts verifier must be a path inside this repository, not {command!r}."
        )
    root = Path(read_root).resolve()
    try:
        verifier = (root / command).resolve(strict=True)
        verifier.relative_to(root)
    except (OSError, ValueError):
        raise TodayPageError(
            f"The Facts verifier is missing or outside the repository: {command}."
        ) from None
    if not verifier.is_file():
        raise TodayPageError(f"The Facts verifier is not a file: {command}.")
    cmd = [str(verifier), *parts[1:]]
    if queue_path is not None:
        cmd.extend(["--queue", str(queue_path)])
    cmd.extend(["--row-key", row_key or "", "--render-date", render_date])
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TodayPageError(f"The Facts verifier failed to run: {command}: {exc}.") from exc
    facts["verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    facts["exit_code"] = result.returncode
    if result.stdout.strip():
        facts["stdout"] = result.stdout.strip()
    if result.stderr.strip():
        facts["stderr"] = result.stderr.strip()
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no detail").strip()
        raise TodayPageError(
            f"The Facts verifier failed for this card ({command}, exit "
            f"{result.returncode}): {detail}"
        )


def _require_fresh_gh_checks(
    core: str, checks: list[dict[str, str]], render_date: str
) -> None:
    """Refuse a card about a pull request or issue with no same-day check."""
    if not _GH_MENTION.search(core):
        return
    if not checks:
        raise TodayPageError(
            "This card refers to a GitHub pull request or issue, so it must "
            "carry the live state from the day it renders, just before "
            "\"Row:\", as \"GH-checked: <owner>/<repo>#<number> <STATE> "
            "updated <timestamp>, checked <YYYY-MM-DD>\"."
        )
    stale = [
        f"{check['repo']}#{check['number']} was checked on "
        f"{check['checked_on']}"
        if check["number"]
        else f"{check['repo']} was checked on {check['checked_on']}"
        for check in checks
        if check["checked_on"] != render_date
    ]
    if stale:
        raise TodayPageError(
            "A GitHub state check must be from the day the document renders "
            f"({render_date}). " + "; ".join(stale) + "."
        )


def _manifest_for_page_id(
    day_directory: Path, page_id: str, fallback: dict[str, Any]
) -> dict[str, Any]:
    """The manifest in one day's archive directory whose page id is page_id.

    A day normally holds one document, and its manifest is found by name. A
    supplement (mc-9jd4t) is a second document for the same day with its own
    name and its own page id, so the answer record can name a page the
    name-derived manifest does not carry. Only that day's own directory is
    searched, one level deep, and the caller's original manifest comes back
    unchanged when nothing matches, so the caller's own refusal still fires.
    """
    try:
        candidates = sorted(Path(day_directory).glob("*.manifest.json"))
    except OSError:
        return fallback
    for candidate in candidates:
        try:
            manifest = _read_json(candidate)
        except Exception:
            continue
        if isinstance(manifest, dict) and manifest.get("page_id") == page_id:
            return manifest
    return fallback


def _split_row_key(ask: str) -> tuple[str, str | None]:
    """Split the trailing "Row: <key>" declaration off one Ask.

    The row's identity used to be derived from its title, project and age, so
    editing a title changed every entry_id under it - which would have
    resurrected a question the reader had discarded (the cross-reviewer, 2026-09-01). The row
    now declares its own key and nothing else feeds identity.
    """
    count = len(re.findall(r"\bRow:", ask))
    if count == 0:
        return ask, None
    if count > 1:
        raise TodayPageError(
            "An Ask can name only one row key, once, just before \"Read:\"."
        )
    match = _ROW_MARKER.search(ask)
    if not match:
        raise TodayPageError(
            "The row key must come just before \"Read:\", as \"Row: <key>\"."
        )
    key = match.group(1)
    if not _QUESTION_KEY.fullmatch(key):
        raise TodayPageError(
            f"The row key must be lowercase letters, digits and underscores, "
            f"not {key!r}."
        )
    return ask[: match.start()].rstrip(), key


def _split_card_specimen(ask: str, root: Path) -> tuple[str, dict[str, str] | None]:
    """Take a specimen placed immediately before the Row marker off an Ask."""
    if "Specimen:" not in ask:
        return ask, None
    if ask.count("Specimen:") != 1:
        raise TodayPageError("A card can show only one font specimen.")
    start = ask.index("Specimen:")
    match = _SPECIMEN_RE.fullmatch(ask[start:])
    if match is None or not ask[:start].strip():
        raise TodayPageError(
            "A card specimen needs a font path and sample text just before Row:."
        )
    source = _validate_specimen_path(match.group(1), root)
    return ask[:start].rstrip(), {
        "path": match.group(1),
        "text": match.group(2),
        "sha256": sha256_file(source),
    }


def row_item_id(row_key: str) -> str:
    """The row's durable identity (contract section 2)."""
    return sha256_bytes(row_key.encode("ascii"))[:12]


def _legacy_item_id(item: str, project: str, age: str) -> str:
    """How a row was identified before it declared its own key.

    Kept only for legacy material. It is derived from wording, so editing a
    title changes it - which is exactly why rows declare a key now.
    """
    return sha256_bytes(f"{item}\0{project}\0{age}".encode())[:12]


def _split_read(ask: str) -> tuple[str, str | None]:
    """Split the trailing "Read: <path>" declaration off one Ask.

    The reader's interview answer 2: the document a question is about belongs behind
    the card, in the same tablet document, in full. The row names it once, at
    the end of the Ask. Nothing else in the Ask may say "Read:", because a
    second one would leave the reader guessing which document the card is
    about.
    """
    count = len(re.findall(r"\bRead:", ask))
    if count == 0:
        return ask, None
    if count > 1:
        raise TodayPageError(
            "An Ask can name only one document to read, once, at the end "
            "(\"Read: <path>\")."
        )
    match = _READ_MARKER.search(ask)
    if not match:
        raise TodayPageError(
            "The document to read must be named at the very end of the Ask, "
            "as \"Read: <repo-relative path>\"."
        )
    return ask[: match.start()].rstrip(), match.group(1)


def _validate_read_path(path: str, root: Path) -> Path:
    """Resolve one Read path under the repository, or say why it cannot be."""
    if path.startswith("/") or path.startswith("~"):
        raise TodayPageError(
            f"The document to read must be a path inside this repository, not "
            f"{path!r}."
        )
    suffix = Path(path).suffix.lower()
    if suffix not in READ_SUFFIXES:
        raise TodayPageError(
            f"The document to read ({path}) is not a text or markdown file, so "
            "it cannot be printed behind the card."
        )
    root = Path(root).resolve()
    try:
        candidate = (root / path).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        raise TodayPageError(
            f"The document to read is missing or outside the repository: {path}."
        ) from None
    if not candidate.is_file():
        raise TodayPageError(
            f"The document to read is not a file: {path}."
        )
    try:
        candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise TodayPageError(
            f"The document to read cannot be read as text: {path}."
        ) from None
    return candidate


def _validate_specimen_path(path: str, root: Path) -> Path:
    """Resolve a TrueType specimen inside the repository before rendering."""
    if Path(path).is_absolute() or path.startswith("~") or Path(path).suffix != ".ttf":
        raise TodayPageError(f"The specimen font must be a repo-relative .ttf file: {path}.")
    root = Path(root).resolve()
    try:
        candidate = (root / path).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        raise TodayPageError(f"The specimen font is missing or outside the repository: {path}.") from None
    if not candidate.is_file():
        raise TodayPageError(f"The specimen font is not a file: {path}.")
    return candidate


def _split_row(line: str) -> list[str] | None:
    """Cells of one table data row, or None if the renderer would skip the line.

    mc-fezd: the ONLY row-detection + cell-split authority. verify-queue and
    waiting-on-reader lint must use this (via validate_row), never their own
    split, so a line cannot parse one way at write time and another at render.
    """
    if not line.startswith("|") or "---" in line or "| Item |" in line:
        return None
    return [cell.strip() for cell in line.strip("|").split("|")]


def validate_row(
    line: str,
    require_keys: bool = True,
    render_date: str | None = None,
    queue_path: Path | None = None,
) -> list[str]:
    """Return card-contract violations for one Active table row (empty = valid).

    Feeds the raw line through the exact render path (_split_row, then
    _decision_copy with the row's REAL title/age/ask), so write-time
    verification and the 06:00 render cannot disagree. Replaces ask-only
    validation, which missed phantom trailing cells (a space after the final
    pipe) and never checked the Age cell (mc-fezd).
    """
    cells = _split_row(line)
    if cells is None:
        return ["not a renderable table row"]
    if len(cells) < 4:
        return ["row has fewer than 4 cells; the renderer would drop it silently"]
    item, project, age = map(clean_markdown, cells[:3])
    ask = re.sub(r"\s+", " ", "|".join(cells[3:])).strip()
    try:
        _decision_copy(
            {"title": item, "project": project, "age": age, "ask": ask},
            require_keys=require_keys,
            render_date=render_date,
            queue_path=queue_path,
        )
    except TodayPageError as exc:
        return [str(exc)]
    except Exception as exc:  # any escape here is itself a generator crash bug
        return [f"parser crash ({type(exc).__name__}): {exc}"]
    return []


def _require_question_recommendations(forms: list[dict[str, Any]]) -> None:
    """Hold a multi-question card whose questions lack their own advice.

    The reader's pen, 2026-09-14, on a two-question card: the card-level
    Recommendation covered question 1 and said nothing about question 2. On a
    card with more than one question the card-level section cannot carry the
    advice, so every question must carry its own "Recommended:" clause. A
    single-question card may use either form, because there the card-level
    section IS that question's recommendation.
    """
    if len(forms) < 2:
        return
    missing = [
        str(index)
        for index, form in enumerate(forms, 1)
        if not form.get("recommendation")
    ]
    if missing:
        raise TodayPageError(
            "This card asks more than one question, so each question needs "
            "its own recommendation. Question "
            + ", ".join(missing)
            + " has none. Write \"Recommended: <answer> because <one "
            "sentence>.\" straight after the question text."
        )


def _decision_copy(
    decision: dict[str, str],
    read_root: Path = REPO_ROOT,
    require_keys: bool = True,
    render_date: str | None = None,
    queue_path: Path | None = None,
) -> dict[str, Any]:
    """Normalize one verified queue row without inferring from free prose.

    require_keys=True holds the row to the current contract: a question key
    and an answer form on every question, and a reading document named at the
    end of the Ask. require_keys=False reads a LEGACY row, which has none of
    those; it exists so archived material and the consumer's legacy path keep
    working, and it must never be used for a row being printed today.
    """
    ask = re.sub(r"\s+", " ", decision.get("ask", "")).strip()
    ask, read_path = _split_read(ask)
    ask, row_key = _split_row_key(ask)
    ask, specimen = _split_card_specimen(ask, read_root)
    ask, fact_check = _split_fact_check(ask)
    ask, ticket_links = _split_ticket_links(ask)
    ask, gh_checks = _split_gh_checks(ask)
    ask, answered_on_row = _split_answered(ask)
    if "Attach:" in ask:
        # The v2 "Attach:" convention is retired (mc-iacbw). It recognised one
        # exact pattern, accepted text files only, and printed an "unavailable"
        # appendix instead of holding the card when the file could not be read.
        # v3 names the reading with "Read:" and holds the row when it is not
        # there. Refusing the old syntax outright means a row cannot declare
        # reading material that silently never gets printed.
        raise TodayPageError(
            "\"Attach:\" is no longer printed. Name the document the card is "
            "about at the end of the Ask, as \"Read: <repo-relative path>\"."
        )
    raw_core = ask
    core = clean_markdown(raw_core)
    if require_keys:
        _refuse_mandatory_read_wording(core, "this card")
        _require_timed_free_space(core)
        _require_fresh_gh_checks(
            core, gh_checks, render_date or dt.date.today().isoformat()
        )
    if core.count("Recommendation:") != 1 or core.count("Decide:") != 1:
        raise TodayPageError(
            "Each Ask needs exactly one Recommendation section and one Decide section."
        )
    if core.index("Decide:") < core.index("Recommendation:"):
        # mc-sguf: this ordering used to escape as a bare ValueError from the
        # split below and crash the 06:00 generator instead of holding the row.
        raise TodayPageError(
            "The Recommendation section must come before the Decide section."
        )
    description, remainder = core.split("Recommendation:", 1)
    recommendation, decision_text = remainder.split("Decide:", 1)
    description = " ".join(_sentences(description.strip()))
    recommendation = clean_markdown(recommendation)
    if require_keys and render_date is not None:
        _require_fresh_facts(
            " ".join(
                [
                    clean_markdown(decision.get("title", "")),
                    description,
                    recommendation,
                    clean_markdown(decision_text),
                ]
            ),
            fact_check,
            render_date,
            row_key,
            read_root,
            queue_path,
        )
    if not recommendation or recommendation[-1] not in ".!?":
        raise TodayPageError("Every recommendation must be a complete sentence.")
    # Parsed from the UNCLEANED Decide section so the question keys survive
    # (clean_markdown eats underscores); _numbered_decisions cleans its own
    # input with the keys protected.
    forms = _numbered_decisions(
        raw_core.split("Decide:", 1)[1], require_keys=require_keys
    )
    # Checked after the questions and the reading, so a row with several
    # problems reports the substantive one first: "this question does not say
    # how it is answered" is more use to the person fixing the row than "this
    # row has no key", and only one reason is reported per row.
    if require_keys:
        _refuse_recorded_answers(forms, answered_on_row)
    if row_key is None and require_keys:
        raise TodayPageError(
            "Every row must declare its own key, just before \"Read:\", as "
            "\"Row: <key>\". Without it the row's identity comes from its "
            "wording, and editing the title would re-ask questions you have "
            "already settled."
        )
    if read_path is None:
        if require_keys:
            raise TodayPageError(
                "Every card must name the document it is about, at the end of "
                "the Ask, as \"Read: <repo-relative path>\"."
            )
        read = None
    else:
        read_file = _validate_read_path(read_path, read_root)
        read = {
            "path": read_path,
            "sha256": sha256_bytes(
                read_file.read_text(encoding="utf-8").encode("utf-8")
            ),
        }
    if require_keys:
        _require_question_recommendations(forms)
    return {
        **decision,
        "row_key": row_key,
        "gh_checks": gh_checks,
        "answered_on_row": answered_on_row,
        "fact_check": fact_check,
        "ticket_links": ticket_links,
        "specimen": specimen,
        "item_id": (
            row_item_id(row_key) if row_key else decision.get("item_id", "")
        ),
        "question": _question_from_title(decision.get("title", "")),
        "dates": _date_line(decision.get("age", "")),
        "description": description,
        "recommendation": recommendation,
        "decision_questions": [form["question"] for form in forms],
        "decision_forms": forms,
        "read": read,
    }


def parse_review_queue(
    path: Path,
    require_keys: bool = True,
    render_date: str | None = None,
) -> list[dict[str, str]]:
    """Return every table row under an Active heading, in source order."""
    source = Path(path)
    return parse_review_queue_text(
        source.read_text(encoding="utf-8"),
        require_keys=require_keys,
        render_date=render_date,
        queue_path=source,
    )


def parse_review_queue_bytes(
    data: bytes,
    require_keys: bool = True,
    render_date: str | None = None,
    queue_path: Path | None = None,
) -> list[dict[str, str]]:
    """Parse rows from queue bytes already in hand.

    A caller that has to both parse the queue and attest to its hash must do
    both from ONE snapshot: reading the file twice lets a writer land between
    the reads, and the manifest then swears to a hash that no longer contains
    the rows it printed (mc-63x2).
    """
    return parse_review_queue_text(
        data.decode("utf-8"),
        require_keys=require_keys,
        render_date=render_date,
        queue_path=queue_path,
    )


def parse_review_queue_text(
    text: str,
    require_keys: bool = True,
    render_date: str | None = None,
    queue_path: Path | None = None,
) -> list[dict[str, str]]:
    """Return every table row under an Active heading, in source order."""
    rows: list[dict[str, str]] = []
    active = False
    for line in text.splitlines():
        if line.startswith("## "):
            active = line.startswith("## Active")
            continue
        if not active:
            continue
        cells = _split_row(line)
        if cells is None or len(cells) < 4:
            continue
        item, project, age = map(clean_markdown, cells[:3])
        ask = re.sub(r"\s+", " ", "|".join(cells[3:])).strip()
        stable = _legacy_item_id(item, project, age)
        rows.append(
            _decision_copy(
                {
                    "item_id": stable,
                    "title": item,
                    "project": project,
                    "age": age,
                    "ask": ask,
                },
                require_keys=require_keys,
                render_date=render_date,
                queue_path=queue_path,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# The already-answered guard (mc-rrjv work item 3)
#
# On 2026-08-16 the reader answered thirty questions with his pen. Nothing consumed
# them, and on 2026-08-19 the generator printed twenty-nine of them straight
# back at him. This guard makes that mechanically impossible: a question with a
# recorded answer is not printed, and the exclusion is named in the manifest so
# it can be audited rather than trusted.
#
# The record is read from the durable sources every run - the pen-answer
# sections of REVIEW-QUEUE.md and the archived resolution files - never from a
# cache, because a cache is one more thing that can quietly go stale and
# re-open the exact hole this closes. If the record cannot be read, generation
# FAILS. A missing document is recoverable in minutes; a re-asked question
# spends the reader's attention on work he already did, which is the failure this
# whole chain exists to stop.
# ---------------------------------------------------------------------------

# REMARKABLE_TODAY_ARCHIVE already overrides this exact default path in
# push-decisions.sh and scheduled-review.sh (mc-mi8xx). generate() had no
# matching override, so a render-only run (no --push) still read prior
# manifests from, and took out a lock file against, the REAL archive under
# REPO_ROOT even when the caller pointed everything else at a scratch
# archive (mc-t5mb6 audit). Honoring the same env var here, with the
# identical default when unset, closes that gap without changing production
# behavior: scheduled-review.sh and morning-dashboard.sh both already set
# REMARKABLE_TODAY_ARCHIVE for every push-decisions.sh/today_page.py child
# they spawn (scheduled-review.sh:289-292, morning-dashboard.sh:350,388) -
# they just set it to the real archive path (their own ARCHIVE_ROOT, which
# itself defaults to this same real path when nothing overrides IT), so a
# normal 05:00 run resolves this constant to exactly the value it always
# has. Only a caller that deliberately points REMARKABLE_TODAY_ARCHIVE at a
# scratch directory (scheduled-review-preflight.sh) changes what this
# constant resolves to.
_ANSWER_RECORD_ROOT_ENV = os.environ.get("REMARKABLE_TODAY_ARCHIVE")
ANSWER_RECORD_ROOT = (
    Path(_ANSWER_RECORD_ROOT_ENV)
    if _ANSWER_RECORD_ROOT_ENV
    else _CONFIG.decisions_archive
)
_PEN_SECTION = re.compile(r"^##\s+Pen\s+(?:answers|dispositions)\b", re.IGNORECASE)
_HEADING_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_ANSWER_LINE = re.compile(
    r"^\s*[-*]\s+(?:\*\*(YES|NO)\*\*|(YES|NO):)\s+(\S.*?)\s*$"
)
_ROW_SUFFIX = re.compile(r"\s+-\s+row:\s+.*$")
_APPENDIX_SUFFIX = re.compile(
    r"\s*Full text:\s*Appendix\s+[A-Z]+,\s*page\s*\d+\.\s*$", re.IGNORECASE
)


class AnswerRecordError(TodayPageError):
    """The record of already-answered questions could not be read.

    Deliberately fatal. Printing a document while blind to what the reader has
    already answered is the failure mode this guard exists to prevent, so an
    unreadable record stops the run instead of degrading to "print anyway".
    """


def normalize_question(text: str) -> str:
    """One comparable form for a decision question.

    Appendix references are appended to a printed question at layout time and
    move with the appendix letters, so they are stripped before comparison: the
    same question is the same question whether or not it carried an appendix
    pointer on the day it was answered.
    """
    value = clean_markdown(text)
    value = _APPENDIX_SUFFIX.sub("", value)
    return value.rstrip("?. ").strip().lower()


def _record_queue_answers(queue_text: str) -> dict[str, dict[str, str]]:
    recorded: dict[str, dict[str, str]] = {}
    section_date: str | None = None
    heading = ""
    for number, line in enumerate(queue_text.splitlines(), 1):
        if line.startswith("## "):
            if _PEN_SECTION.match(line):
                found = _HEADING_DATE.search(line)
                if not found:
                    raise AnswerRecordError(
                        f"The pen-answer section on line {number} of the review "
                        f"queue carries no date: {line.strip()!r}. No document "
                        "was generated, because a question already answered "
                        "cannot be identified."
                    )
                section_date = found.group(1)
                heading = line.strip()
            else:
                section_date = None
            continue
        if section_date is None:
            continue
        match = _ANSWER_LINE.match(line)
        if not match:
            continue
        choice = match.group(1) or match.group(2)
        question = normalize_question(_ROW_SUFFIX.sub("", match.group(3)))
        if not question:
            continue
        recorded.setdefault(
            question,
            {
                "answered_on": section_date,
                "choice": choice.upper(),
                "source": f"REVIEW-QUEUE.md {heading}",
            },
        )
    return recorded


def _record_archived_answers(archive_root: Path) -> dict[str, dict[str, str]]:
    recorded: dict[str, dict[str, str]] = {}
    root = Path(archive_root)
    if not root.is_dir():
        raise AnswerRecordError(
            f"The archived pen-answer record is missing at {root}. No document "
            "was generated: without it an answered question could be re-asked."
        )
    for day in sorted(root.iterdir()):
        if not day.is_dir():
            continue
        try:
            dt.date.fromisoformat(day.name)
        except ValueError:
            continue
        resolution = day / "processed" / "resolution.json"
        if not resolution.exists():
            continue
        try:
            payload = json.loads(resolution.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AnswerRecordError(
                f"The archived pen answers for {day.name} could not be read "
                f"({exc}). No document was generated."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("decisions"), list
        ):
            raise AnswerRecordError(
                f"The archived pen answers for {day.name} are not in the "
                "expected shape. No document was generated."
            )
        # A v3 document (layout decisions-v3.x, first pen round 2026-09-02)
        # records its answers in question-state.json, which
        # filter_settled_questions reads under the queue lock. Its resolution
        # also carries DEFER and DISCARD boxes and write-in answers with no
        # box (choice None); none of those is an answer in this record's
        # sense, and reading them here would drop a deferred card or stop
        # the render on a write-in. Skip the day; the state file governs it.
        layout = payload.get("layout_version")
        if isinstance(layout, str) and layout.startswith("decisions-v3"):
            continue
        for decision in payload["decisions"]:
            if (
                not isinstance(decision, dict)
                or not isinstance(decision.get("label"), str)
                or not isinstance(decision.get("choice"), str)
            ):
                raise AnswerRecordError(
                    f"The archived pen answers for {day.name} contain an "
                    "unreadable answer. No document was generated."
                )
            question = normalize_question(decision["label"])
            if question:
                recorded.setdefault(
                    question,
                    {
                        "answered_on": day.name,
                        "choice": decision["choice"].upper(),
                        "source": str(resolution),
                    },
                )
    return recorded


def load_recorded_answers(
    queue_text: str,
    archive_root: Path = ANSWER_RECORD_ROOT,
) -> dict[str, dict[str, str]]:
    """Every question that already carries an answer, keyed for comparison."""
    recorded = _record_archived_answers(archive_root)
    for question, record in _record_queue_answers(queue_text).items():
        recorded[question] = record
    return recorded


def filter_already_answered(
    decisions: list[dict[str, Any]],
    recorded: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into the ones still to ask and the ones already answered.

    A row is excluded whole. Its questions belong to one card and one queue
    row; printing the remainder of a card whose first question the reader already
    answered puts him back in front of a decision he closed.
    """
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for decision in decisions:
        match = None
        for question in decision.get("decision_questions", []):
            record = recorded.get(normalize_question(question))
            if record:
                match = (question, record)
                break
        if match is None:
            kept.append(decision)
            continue
        question, record = match
        excluded.append(
            {
                "item_id": decision.get("item_id", ""),
                "title": clean_markdown(str(decision.get("title", ""))),
                "question": clean_markdown(question),
                "answered_on": record["answered_on"],
                "choice": record["choice"],
                "source": record["source"],
            }
        )
    return kept, excluded


# Same rationale and same env var as ANSWER_RECORD_ROOT above (mc-t5mb6): the
# question-state file sits one directory above today-pages in the real
# archive, so when REMARKABLE_TODAY_ARCHIVE is set this follows it there
# (archive/parent + question-state.json) rather than the real archive.
QUESTION_STATE_PATH = (
    Path(_ANSWER_RECORD_ROOT_ENV).parent / "question-state.json"
    if _ANSWER_RECORD_ROOT_ENV
    else _CONFIG.question_state
)
_SETTLED_STATES = ("answered", "discarded")


def load_settled_question_history(
    state: dict[str, Any],
    archive_root: Path = ANSWER_RECORD_ROOT,
) -> dict[str, dict[str, str]]:
    """Bind terminal state ids to the row keys and text the reader saw.

    Question keys survive wording edits, but an editor must not renumber a
    different open question onto a key that is already answered or discarded.
    The source page manifest is the durable record that lets the generator
    tell those two cases apart.
    """
    requested = {
        entry_id: record
        for entry_id, record in (state.get("entries") or {}).items()
        if record.get("state") in _SETTLED_STATES
    }
    history: dict[str, dict[str, str]] = {}
    skipped_entries: set[str] = set()
    loaded_sources: set[tuple[str, str]] = set()
    root = Path(archive_root).resolve()
    for entry_id, record in requested.items():
        day = str(record.get("since", ""))
        source = str(record.get("source", ""))
        source_key = (day, source)
        if source_key in loaded_sources:
            continue
        loaded_sources.add(source_key)
        if source.startswith("status-dashboard-"):
            embedded = record.get("history")
            if not isinstance(embedded, dict) or any(
                not isinstance(embedded.get(key), str) or not embedded.get(key)
                for key in ("item_id", "question_key", "question")
            ):
                print(
                    "Settled-question history from the status dashboard is incomplete. "
                    f"Entry {entry_id!r} was skipped; the rest of the document will continue.",
                    file=sys.stderr,
                )
                skipped_entries.add(entry_id)
                continue
            history[entry_id] = dict(embedded)
            continue
        try:
            dt.date.fromisoformat(day)
        except ValueError as exc:
            raise TodayPageError(
                f"Settled-question history names an invalid date ({day!r}). "
                "No document was generated."
            ) from exc
        manifest_path = (
            root / day / f"decisions-{day}.manifest.json"
        ).resolve()
        if root not in manifest_path.parents:
            raise TodayPageError(
                "Settled-question history points outside its archive. "
                "No document was generated."
            )
        try:
            manifest = _read_json(manifest_path)
        except Exception as exc:
            raise TodayPageError(
                f"Settled-question history could not be read for {day} ({exc}). "
                "No document was generated."
            ) from exc
        if manifest.get("page_id") != source:
            # A day can hold more than one document (a supplement carrying
            # cards the main document was missing, mc-9jd4t), and an answer
            # penned on the second one names a page id the day's main
            # manifest does not carry. Look for the manifest that does,
            # inside that day's directory only, before refusing.
            manifest = _manifest_for_page_id(root / day, source, manifest)
        if manifest.get("page_id") != source:
            raise TodayPageError(
                f"Settled-question history for {day} does not match its "
                "source page. No document was generated."
            )
        for entry in manifest.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("kind") != "decision":
                continue
            entry_id = entry.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                continue
            state_record = requested.get(entry_id)
            if not state_record or state_record.get("source") != source:
                continue
            history[entry_id] = {
                "item_id": str(entry.get("item_id", "")),
                "question_key": str(entry.get("question_key", "")),
                "question": clean_markdown(str(entry.get("label", ""))),
            }
    missing = sorted(set(requested) - set(history) - skipped_entries)
    if missing:
        raise TodayPageError(
            "Settled-question history is missing "
            f"{len(missing)} recorded question(s). No document was generated."
        )
    return history


def _settled_for_render(
    record: dict[str, Any] | None, render_date: str | None
) -> bool:
    """Is this question off today's document?

    "answered" and "discarded" are terminal and never print again. "deferred"
    is not terminal, but it is off THIS day: the reader 2026-09-07, his words, "if
    my decision is to defer, then it does not get put into the follow up
    document the same day, but rather it actually gets deferred". A card he
    deferred this morning reappearing on the follow-up document this afternoon
    is the failure that rule names, so a deferred record whose `since` is the
    date being rendered is treated as settled for this render only. Tomorrow
    its `since` is no longer the render date and it prints again unchanged.
    """
    if not record:
        return False
    state = record.get("state")
    if state in _SETTLED_STATES:
        return True
    if state != "deferred" or not render_date:
        return False
    return str(record.get("since", "")) == render_date


def filter_settled_questions(
    decisions: list[dict[str, Any]],
    state: dict[str, Any],
    history: dict[str, dict[str, str]],
    render_date: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Leave off cards the reader has fully settled (contract section 4).

    "answered" and "discarded" are terminal: an answered question is done, and
    a discarded one is never to be asked again, even if the row's wording
    changes. A question deferred ON `render_date` is off this day's document
    and comes back tomorrow (see _settled_for_render). Everything else prints:
    "needs_clarification" stays in front of him, "unanswered" has not been
    asked yet.

    A partially settled row cannot be shortened safely here. Its title, body,
    and recommendation may depend on the removed question, so printing only
    the remaining form can produce a dangling conditional or re-present a
    decision the reader already made. The row author must rewrite the complete Ask
    around the open questions before the document can render.

    A row with nothing left to print is not a held row - it is settled, and it
    should already have left Active - so it is recorded separately from the
    holds on the cover page.
    """
    records = state.get("entries") or {}
    kept: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for decision in decisions:
        all_keys = [form["question_key"] for form in decision["decision_forms"]]
        item_history = {
            prior["question_key"]: (entry_id, records[entry_id], prior)
            for entry_id, prior in history.items()
            if prior["item_id"] == decision["item_id"]
        }
        for key, (_entry_id, record, prior) in item_history.items():
            if key in all_keys:
                continue
            excluded.append(
                {
                    "item_id": decision["item_id"],
                    "title": clean_markdown(str(decision.get("title", ""))),
                    "question": prior["question"],
                    "answered_on": str(record["since"]),
                    "choice": str(record["state"]).upper(),
                    "source": str(record["source"]),
                }
            )
        for form in decision["decision_forms"]:
            key = form["question_key"]
            prior_record = item_history.get(key)
            if prior_record is None:
                continue
            _entry_id, _record, prior = prior_record
            current_question = clean_markdown(str(form["question"]))
            if normalize_question(prior["question"]) == normalize_question(
                current_question
            ):
                continue
            title = (
                clean_markdown(str(decision.get("title", "")))
                or decision["item_id"]
            )
            raise TodayPageError(
                f'"{title}" reuses settled question key {key!r}. It previously '
                f"meant: {prior['question']} It now means: {current_question} "
                "Keep the original key with its question, and give different "
                "text a new key."
            )
        printable = [
            form
            for form in decision["decision_forms"]
            if not _settled_for_render(
                records.get(
                    _entry_identity(decision["item_id"], form["question_key"])
                ),
                render_date,
            )
        ]
        if printable and len(printable) != len(decision["decision_forms"]):
            suppressed = [
                clean_markdown(str(form["question"]))
                for form in decision["decision_forms"]
                if form not in printable
            ]
            title = (
                clean_markdown(str(decision.get("title", "")))
                or decision["item_id"]
            )
            open_keys = [form["question_key"] for form in printable]
            raise TodayPageError(
                f'"{title}" still contains settled question(s): '
                + "; ".join(suppressed)
                + ". Rewrite this queue row so its title, explanation, "
                "recommendation, and questions describe only what remains open "
                "(a question deferred today counts as settled until tomorrow). "
                "Keep these question keys unchanged: "
                + ", ".join(open_keys)
                + "."
            )
        if not printable:
            for form in decision["decision_forms"]:
                entry_id = _entry_identity(
                    decision["item_id"], form["question_key"]
                )
                record = records[entry_id]
                excluded.append(
                    {
                        "item_id": decision["item_id"],
                        "title": clean_markdown(str(decision.get("title", ""))),
                        "question": clean_markdown(str(form["question"])),
                        "answered_on": str(record["since"]),
                        "choice": str(record["state"]).upper(),
                        "source": str(record["source"]),
                    }
                )
            states = {
                str(
                    records[
                        _entry_identity(
                            decision["item_id"], form["question_key"]
                        )
                    ]["state"]
                )
                for form in decision["decision_forms"]
            }
            reason = (
                "every question deferred today; it returns tomorrow"
                if states == {"deferred"}
                else "every question answered or discarded"
                if "deferred" not in states
                else "every question answered, discarded, or deferred today"
            )
            settled.append(
                {
                    "item_id": decision["item_id"],
                    "title": clean_markdown(str(decision.get("title", ""))),
                    "reason": reason,
                }
            )
            continue
        kept.append({**decision, "all_question_keys": all_keys})
    return kept, settled, excluded


def close_review_queue_row(path: Path, item_id: str) -> None:
    """Move one exact Active row to Closed after its approval is recorded.

    Reading, editing and replacing the file is ONE operation and is held under
    the shared queue lock for its whole length; anything less loses a
    concurrent writer's work (mc-63x2).
    """
    with queue_lock(path):
        _close_review_queue_row_locked(path, item_id)


def _close_review_queue_row_locked(path: Path, item_id: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    active = False
    target_index = None
    target_cells = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            active = line.startswith("## Active")
            continue
        if not active:
            continue
        cells = _split_row(line)
        if cells is None or len(cells) < 4:
            continue
        ask = re.sub(r"\s+", " ", "|".join(cells[3:])).strip()
        try:
            _, row_key = _split_row_key(_split_read(ask)[0])
        except TodayPageError:
            row_key = None
        stable = (
            row_item_id(row_key)
            if row_key
            else _legacy_item_id(
                clean_markdown(cells[0]),
                clean_markdown(cells[1]),
                clean_markdown(cells[2]),
            )
        )
        if stable == item_id:
            target_index = index
            target_cells = cells
            break
    if target_index is None or target_cells is None:
        raise TodayPageError(
            "The approved review item is no longer active. No queue change was made."
        )
    del lines[target_index]
    closed = next(
        (index for index, line in enumerate(lines) if line.startswith("## Closed")),
        None,
    )
    if closed is None:
        raise TodayPageError("The review queue has no Closed section.")
    separator = next(
        (
            index
            for index in range(closed + 1, min(closed + 8, len(lines)))
            if lines[index].startswith("|---")
        ),
        None,
    )
    if separator is None:
        raise TodayPageError("The Closed table could not be found.")
    original_ask = "|".join(target_cells[3:]).strip()
    today = dt.date.today().isoformat()
    closed_cells = [
        escape_table_cell(target_cells[0]),
        today,
        escape_table_cell(
            f"DECIDED by Decisions document {today}: approved. "
            f"Original ask: {original_ask}"
        ),
    ]
    closed_row = "| " + " | ".join(closed_cells) + " |"
    lines.insert(separator + 1, closed_row)
    atomic_text(path, "\n".join(lines) + "\n")


def validate_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise TodayPageError("Each action must be a JSON object.")
    action_type = action.get("type")
    if action_type not in ALLOWED_ACTIONS:
        raise TodayPageError(f"Action type is not allowed: {action_type!r}.")
    payload = action.get("payload")
    if not isinstance(payload, dict):
        raise TodayPageError(f"{action_type} requires a payload object.")
    required = ALLOWED_ACTIONS[action_type] - {"note"}
    missing = required - payload.keys()
    extra = payload.keys() - ALLOWED_ACTIONS[action_type]
    if missing or extra:
        raise TodayPageError(
            f"Invalid {action_type} payload. Missing: {sorted(missing)}; "
            f"unexpected: {sorted(extra)}."
        )
    for key, value in payload.items():
        if key == "days":
            if not isinstance(value, int) or not 1 <= value <= 30:
                raise TodayPageError("Defer days must be an integer from 1 through 30.")
        elif not isinstance(value, str) or not value.strip():
            raise TodayPageError(f"{action_type}.{key} must be a non-empty string.")
    label = action.get("label")
    if not isinstance(label, str) or not label.strip():
        raise TodayPageError(f"{action_type} requires a plain-English label.")
    return {"type": action_type, "payload": payload, "label": clean_markdown(label)}


def load_actions(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TodayPageError("The actions file must contain a JSON list.")
    if len(raw) > 5:
        raise TodayPageError("The Decisions document allows no more than five actions.")
    return [validate_action(action) for action in raw]


# ---------------------------------------------------------------------------
# Card system labels (mc-rrjv work item 2)
#
# The reader's instruction, written on the 2026-08-16 sheet: "You need to label each
# question card with the system/feature/component it's about." The label comes
# from data that already exists - the review queue's own Project column first,
# the owning beads repository second - and prints NOTHING when neither source
# has an answer. Inventing a taxonomy here would put a guess on the page in the
# place where the reader is entitled to read a fact.
# ---------------------------------------------------------------------------

_BEADS_PREFIX_CACHE: dict[str, str] | None = None
_TICKET_ID = re.compile(r"\(([a-z][a-z0-9]{1,7}-[a-z0-9][a-z0-9-]{1,39})\)")


def _project_directory_name(directory: Path) -> str:
    """The project's own name, even when this checkout is a git worktree.

    A worktree's directory is named for the branch being worked on, not for the
    project, so labelling a card from it would print the wrong system name.
    ".git" is a file in a worktree and points back at the main checkout; that
    path is the project.
    """
    marker = directory / ".git"
    try:
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                for parent in gitdir.parents:
                    if parent.name == ".git":
                        return parent.parent.name
    except (OSError, UnicodeError, ValueError):
        pass
    return directory.name


def _beads_prefix_projects(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Map each beads issue-id prefix to the project directory that owns it.

    Beads records no project field on an issue (verified against
    .beads/issues.jsonl and `bd show --json`); the owning repository IS the
    project, and the id prefix is what names it. The map is read from the
    repositories beads is configured to hydrate, so it is observed data rather
    than a hard-coded table. Any failure returns an empty map: a missing label
    prints nothing, which is the documented fallback.
    """
    global _BEADS_PREFIX_CACHE
    if _BEADS_PREFIX_CACHE is not None:
        return _BEADS_PREFIX_CACHE
    mapping: dict[str, str] = {}
    try:
        config = (repo_root / ".beads" / "config.yaml").read_text(encoding="utf-8")
        directories = [repo_root]
        for match in re.finditer(r'^\s*-\s*"?([^"\n]+)"?\s*$', config, re.MULTILINE):
            candidate = Path(match.group(1).strip())
            if candidate.is_absolute():
                directories.append(candidate)
        for directory in directories:
            issues = directory / ".beads" / "issues.jsonl"
            with issues.open(encoding="utf-8") as handle:
                first = handle.readline()
            issue_id = json.loads(first).get("id", "")
            prefix = str(issue_id).split("-", 1)[0]
            if prefix:
                mapping.setdefault(prefix, _project_directory_name(directory))
    except (OSError, ValueError, UnicodeError):
        mapping = {}
    _BEADS_PREFIX_CACHE = mapping
    return mapping


def card_system_label(
    decision: dict[str, Any],
    ticket_projects: dict[str, str] | None = None,
) -> str:
    """The system/feature this card is about, or "" when nothing says."""
    project = clean_markdown(str(decision.get("project") or ""))
    if project:
        return project
    if ticket_projects is None:
        ticket_projects = _beads_prefix_projects()
    if not ticket_projects:
        return ""
    haystack = " ".join(
        str(decision.get(field) or "") for field in ("title", "ask")
    ).lower()
    for match in _TICKET_ID.finditer(haystack):
        prefix = match.group(1).split("-", 1)[0]
        if prefix in ticket_projects:
            return ticket_projects[prefix]
    return ""


def box(cx: float, cy: float, size: float = BOX_SIZE) -> list[float]:
    half = size / 2
    return [
        round(cx - half, 2),
        round(cy - half, 2),
        round(cx + half, 2),
        round(cy + half, 2),
    ]


def _text_width(text: str, font: str, size: float) -> float:
    """Conservative built-in Helvetica metrics for dependency-free manifests."""
    width_units = 0.0
    for character in text:
        if character == " ":
            width_units += 0.28
        elif character in "ilI.,'`!:;|":
            width_units += 0.28
        elif character in "MW@%&":
            width_units += 0.92
        elif character in "mw":
            width_units += 0.80
        elif character.isupper():
            width_units += 0.68
        else:
            width_units += 0.57
    if "Bold" in font:
        width_units *= 1.04
    return width_units * size


def _wrap_text(text: str, font: str, size: float, width: float) -> list[str]:
    """Wrap every character without shrinking or ellipsizing."""
    words = clean_markdown(text).split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        chunks: list[str] = []
        remaining = word
        while remaining and _text_width(remaining, font, size) > width:
            split_at = 1
            while (
                split_at < len(remaining)
                and _text_width(remaining[: split_at + 1], font, size) <= width
            ):
                split_at += 1
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        for chunk in chunks:
            candidate = chunk if not current else f"{current} {chunk}"
            if current and _text_width(candidate, font, size) > width:
                lines.append(current)
                current = chunk
            else:
                current = candidate
    if current:
        lines.append(current)
    return lines


def _appendix_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


# --- Appendix markdown rendering (mc-vjz5) ------------------------------
#
# Attachments are rendered as styled line records, not raw text, so headings,
# bullets, code, tables, and quotes read as structure instead of literal
# markdown characters. Wrapping happens here (manifest build time) using
# reportlab's real string-width measurement, not at render time, because the
# manifest is the single source of truth for pagination and the canvas does
# not exist yet. reportlab stays an optional import: if it is genuinely
# unavailable, `_string_width` falls back to the old width*0.6 ratio estimate
# rather than failing the build - but that fallback is not the normal path.

# Built from code points, not literals: this repository's source is ASCII
# only, and a smart quote pasted into a table like this is invisible in a
# diff. The keys are the characters this map exists to remove.
_ASCII_TRANSLITERATIONS = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "--", "\u2015": "--",
    "\u2026": "...", "\u00a0": " ", "\u2022": "*",
    "\u2039": "<", "\u203a": ">", "\u00ab": "<<", "\u00bb": ">>",
}


def _transliterate_typography(text: str) -> str:
    """Common typographic Unicode -> ASCII, so one curly quote doesn't blank a doc."""
    for source, replacement in _ASCII_TRANSLITERATIONS.items():
        text = text.replace(source, replacement)
    return "".join(character if ord(character) <= 127 else "?" for character in text)


def _string_width(text: str, font_name: str, font_size: float) -> float:
    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth
    except ImportError:
        return len(text) * font_size * 0.6
    try:
        return stringWidth(text, font_name, font_size)
    except Exception:
        return len(text) * font_size * 0.6


def _split_long_word(
    word: str, font_name: str, font_size: float, max_width: float
) -> list[str]:
    """Char-chunk a single word wider than max_width. The only mid-word split case."""
    chunks: list[str] = []
    current = ""
    for character in word:
        candidate = current + character
        if current and _string_width(candidate, font_name, font_size) > max_width:
            chunks.append(current)
            current = character
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [word]


def _wrap_words(
    text: str, font_name: str, font_size: float, max_width: float
) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if _string_width(word, font_name, font_size) > max_width:
            if current:
                lines.append(current)
                current = ""
            chunks = _split_long_word(word, font_name, font_size, max_width)
            lines.extend(chunks[:-1])
            current = chunks[-1] if chunks else ""
            continue
        candidate = f"{current} {word}" if current else word
        if _string_width(candidate, font_name, font_size) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _appendix_available_width(style: str, indent: int, marker: str | None) -> float:
    width = PAGE_W - 2 * PAGE_MARGIN_X - indent * APPENDIX_INDENT_UNIT
    if marker is not None or style in _APPENDIX_HANGING_STYLES:
        width -= APPENDIX_MARKER_GAP
    return max(20.0, width)


# The reader's contract puts each card's reading behind it in full, and those
# documents are dated research files: the blume appendix cites version 1.6.4
# while the card correctly says 1.6.5 is current today. Rewriting them would
# falsify a dated record, so each appendix instead opens with one line saying
# when it was written and which of the two to trust (the reader, 2026-09-13).
def _document_written_on(source: Path) -> str:
    """The date a reading document was last written, for its as-of line."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(source)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        stamp = result.stdout.strip()
        if result.returncode == 0 and stamp:
            dt.date.fromisoformat(stamp)
            return stamp
    except Exception:
        pass
    return dt.date.fromtimestamp(source.stat().st_mtime).isoformat()


def _appendix_as_of_records(source: Path) -> list[dict[str, Any]]:
    """The one-line as-of note that opens every appendix."""
    written = _document_written_on(source)
    return [
        _appendix_record(
            f"Written {_human_date(written)}. Where a number here differs "
            "from the card, the card is current as of today.",
            "body",
        ),
        _appendix_blank_record(),
    ]


def _appendix_record(
    text: str, style: str, indent: int = 0, marker: str | None = None
) -> dict[str, Any]:
    return {
        "text": text,
        "style": style,
        "indent": indent,
        "marker": marker,
        "height": _APPENDIX_STYLE_HEIGHTS.get(style, APPENDIX_LINE_HEIGHT),
    }


def _appendix_blank_record(before_heading: bool = False) -> dict[str, Any]:
    return {
        "text": "",
        "style": "blank",
        "indent": 0,
        "marker": None,
        "height": APPENDIX_HEADING_GAP if before_heading else APPENDIX_BLOCK_GAP,
    }


# One optional space between "]" and "(": the research documents wrap long
# links across a source line break, and the paragraph collector joins those
# lines with a space before this runs. Strict CommonMark would print such a
# link literally - which is exactly the "[#156 ...] (https://...)" that showed
# up on page 37 of the 2026-09-03 document.
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\] ?\(([^)]*)\)")
_INLINE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC_RE = re.compile(r"\*(.+?)\*")
# Underscore emphasis is stripped only at word boundaries (mc-dsb68). `_` is
# common inside code-like identifiers (close_ticket, approve_queue_row), and
# CommonMark's intraword-underscore rule says those are not emphasis - the
# lookaround pair is exactly that rule, so "_italic_" loses its markers while
# "close_ticket" keeps every character.
_INLINE_UNDERSCORE_BOLD_RE = re.compile(
    r"(?<![A-Za-z0-9_])__(\S(?:.*?\S)?)__(?![A-Za-z0-9_])"
)
_INLINE_UNDERSCORE_ITALIC_RE = re.compile(
    r"(?<![A-Za-z0-9_])_(\S(?:[^_]*?\S)?)_(?![A-Za-z0-9_])"
)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _strip_inline_markdown(text: str) -> str:
    """Strip inline markers so they never print as literal characters.

    A clean strip, not a bold font run - see mc-vjz5 task notes: a messy
    half-render is worse than a plain strip. Link URLs are pulled out
    separately by `_extract_links` before this runs; stripping here is the
    belt-and-braces path for callers that do not need the URLs back.
    """
    text = _INLINE_LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _INLINE_BOLD_RE.sub(r"\1", text)
    text = _INLINE_UNDERSCORE_BOLD_RE.sub(r"\1", text)
    text = _INLINE_ITALIC_RE.sub(r"\1", text)
    text = _INLINE_UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    return text


def _extract_links(text: str) -> tuple[str, list[str]]:
    """Replace `[text](url)` with its text and hand the URLs back in order.

    The tablet has no tap target, so a printed "(https://...)" inside a
    sentence is noise the reader has to read past. The URL still has to be there -
    he types them - so it goes on its own indented line under the paragraph.
    Bare URLs are left exactly as written; only bracketed links are rewritten.
    """
    urls: list[str] = []

    def replace(match: re.Match[str]) -> str:
        target = match.group(2).strip()
        if target:
            # `[text](url "title")` - the title is not a destination.
            head = target.split()[0].strip("<>")
            if head and head not in urls:
                urls.append(head)
        return match.group(1)

    return _INLINE_LINK_RE.sub(replace, text), urls


def _link_records(urls: list[str], indent: int) -> list[dict[str, Any]]:
    font_name, font_size = _APPENDIX_STYLE_FONTS["link"]
    available = _appendix_available_width("link", indent, None)
    records: list[dict[str, Any]] = []
    for url in urls:
        for line_text in _wrap_words(url, font_name, font_size, available):
            records.append(_appendix_record(line_text, "link", indent))
    return records


def _wrap_text_block(
    raw_text: str,
    style: str,
    indent: int = 0,
    marker: str | None = None,
    marker_every_line: bool = False,
) -> list[dict[str, Any]]:
    linked, urls = _extract_links(raw_text)
    clean = _strip_inline_markdown(linked)
    font_name, font_size = _APPENDIX_STYLE_FONTS[style]
    available = _appendix_available_width(style, indent, marker)
    wrapped = _wrap_words(clean, font_name, font_size, available)
    records = []
    for line_index, line_text in enumerate(wrapped):
        this_marker = marker if marker is not None and (line_index == 0 or marker_every_line) else None
        records.append(_appendix_record(line_text, style, indent, this_marker))
    records.extend(_link_records(urls, indent + 1))
    return records


def _wrap_preformatted_line(
    line: str, style: str, indent: int = 0
) -> list[dict[str, Any]]:
    """Code/table lines: preserve as-is, hard-wrap only if wider than the page."""
    font_name, font_size = _APPENDIX_STYLE_FONTS[style]
    max_width = _appendix_available_width(style, indent, marker=None)
    if not line:
        return [_appendix_record("", style, indent)]
    if _string_width(line, font_name, font_size) <= max_width:
        return [_appendix_record(line, style, indent)]
    chunks = _split_long_word(line, font_name, font_size, max_width)
    return [_appendix_record(chunk, style, indent) for chunk in chunks]


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_RULE_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)(.*)$")


def _split_table_cells(row: str) -> list[str]:
    """Split a pipe row into cells, treating a pipe inside a code span as text.

    The fourth read of 2026-09-03 found `curl ... | sh` inside a cell split
    into two columns. Pipes inside backticks are hidden before the split and
    restored in each cell afterwards.
    """
    placeholder = "\x00PIPE\x00"
    protected = _INLINE_CODE_RE.sub(
        lambda m: "`" + m.group(1).replace("|", placeholder) + "`", row
    )
    return [
        cell.replace(placeholder, "|") for cell in protected.strip("|").split("|")
    ]


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    # A pipe inside a code span is text, not a column break: the fourth read
    # of 2026-09-03 found a prose sentence carrying `curl ... | sh` shredded
    # into two columns (moli reading, pages 54, 56 and 58). The split below
    # hides those pipes, so such a line yields a single cell and is prose.
    cells = [cell.strip() for cell in _split_table_cells(stripped)]
    return len(cells) >= 2


def _is_table_separator_row(row: str) -> bool:
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    named = [cell for cell in cells if cell]
    return bool(named) and all(re.fullmatch(r":?-{1,}:?", cell) for cell in named)


def _table_matrix(rows: list[str]) -> list[list[str]]:
    """Pipe rows -> a rectangular cell matrix, separator row dropped.

    Inline markers are stripped here, not at draw time: the cell text that
    gets measured for column widths has to be the text that prints, or the
    columns are sized for characters nobody sees.
    """
    matrix: list[list[str]] = []
    for row in rows:
        stripped = row.strip()
        if _is_table_separator_row(stripped):
            continue
        cells = [
            _strip_inline_markdown(_INLINE_LINK_RE.sub(r"\1 \2", cell.strip()))
            for cell in _split_table_cells(stripped)
        ]
        matrix.append(cells)
    if not matrix:
        return []
    columns = max(len(cells) for cells in matrix)
    for cells in matrix:
        cells.extend([""] * (columns - len(cells)))
    return matrix


def _table_column_widths(matrix: list[list[str]]) -> tuple[list[float], list[float]]:
    """Per column: the width that needs no wrapping, and the width below
    which a word would have to be split mid-word."""
    columns = len(matrix[0])
    natural = [0.0] * columns
    minimum = [APPENDIX_TABLE_MIN_COL_WIDTH] * columns
    for row_index, cells in enumerate(matrix):
        style = "table_header" if row_index == 0 else "table"
        font_name, font_size = _APPENDIX_STYLE_FONTS[style]
        for column, cell in enumerate(cells):
            natural[column] = max(
                natural[column], _string_width(cell, font_name, font_size)
            )
            for word in cell.split():
                minimum[column] = max(
                    minimum[column], _string_width(word, font_name, font_size)
                )
    natural = [max(natural[i], minimum[i]) for i in range(columns)]
    return natural, minimum


def _allocate_table_widths(
    natural: list[float], minimum: list[float], usable: float
) -> list[float] | None:
    """Share `usable` across columns in proportion to their natural width,
    never below a column's longest word. None means the table does not fit."""
    if usable <= 0 or not natural:
        return None
    if sum(natural) <= usable:
        return list(natural)
    if sum(minimum) > usable:
        return None
    widths = [0.0] * len(natural)
    free = list(range(len(natural)))
    remaining = usable
    while free:
        total = sum(natural[i] for i in free)
        if total <= 0:
            share = remaining / len(free)
            for index in free:
                widths[index] = max(share, minimum[index])
            break
        pinned = [
            index
            for index in free
            if remaining * natural[index] / total < minimum[index]
        ]
        if not pinned:
            for index in free:
                widths[index] = remaining * natural[index] / total
            break
        for index in pinned:
            widths[index] = minimum[index]
            remaining -= minimum[index]
            free.remove(index)
    return widths


def _table_row_record(
    cells: list[str],
    widths: list[float],
    offsets: list[float],
    header: bool,
    rule: bool,
    group: str,
) -> dict[str, Any]:
    """One whole table row as ONE line record.

    One record per row is what keeps a row off a page boundary: pagination
    moves records, so a row that does not fit moves entire (mc-dsb68).
    """
    style = "table_header" if header else "table"
    font_name, font_size = _APPENDIX_STYLE_FONTS[style]
    wrapped = [
        _wrap_words(cell, font_name, font_size, widths[index])
        for index, cell in enumerate(cells)
    ]
    line_count = max((len(lines) for lines in wrapped), default=1)
    record = _appendix_record(" | ".join(cells), style, 0)
    record["height"] = line_count * APPENDIX_TABLE_LINE_HEIGHT + APPENDIX_TABLE_ROW_PAD
    record["line_height"] = APPENDIX_TABLE_LINE_HEIGHT
    record["cells"] = wrapped
    record["columns"] = [
        {"x": offsets[index], "width": widths[index]} for index in range(len(cells))
    ]
    record["header"] = header
    record["rule"] = rule
    record["group"] = group
    return record


def _table_labelled_records(
    matrix: list[list[str]], group_prefix: str
) -> list[dict[str, Any]]:
    """The too-wide fallback: each row as "Header: cell", one per line.

    Squeezing eight columns onto a 552-point page produces columns two
    characters wide - unreadable, and it was overflowing the margin anyway.
    A labelled block is longer but every value is legible and still attached
    to its heading.
    """
    headers = matrix[0]
    records: list[dict[str, Any]] = []
    for row_index, cells in enumerate(matrix[1:], start=1):
        row_records: list[dict[str, Any]] = []
        for column, cell in enumerate(cells):
            label = headers[column] if column < len(headers) else ""
            text = f"{label}: {cell}" if label else cell
            if not text.strip().strip(":"):
                continue
            row_records.extend(_wrap_text_block(text, "table", indent=1))
        if not row_records:
            continue
        group = f"{group_prefix}-row{row_index}"
        for record in row_records:
            record["group"] = group
        if records:
            records.append(_appendix_blank_record())
        records.extend(row_records)
    if not records:
        # A header-only table has no rows to label; print the headings.
        records.extend(_wrap_text_block(" | ".join(headers), "table_header", indent=0))
    return records


def _table_records(rows: list[str], group_prefix: str) -> list[dict[str, Any]]:
    matrix = _table_matrix(rows)
    if not matrix:
        return [_appendix_record("", "table", 0)]
    columns = len(matrix[0])
    available = _appendix_available_width("table", 0, None)
    usable = available - APPENDIX_TABLE_COL_GAP * (columns - 1)
    natural, minimum = _table_column_widths(matrix)
    widths = _allocate_table_widths(natural, minimum, usable)
    if widths is None:
        return _table_labelled_records(matrix, group_prefix)
    offsets: list[float] = []
    cursor = 0.0
    for width in widths:
        offsets.append(cursor)
        cursor += width + APPENDIX_TABLE_COL_GAP
    records: list[dict[str, Any]] = []
    for row_index, cells in enumerate(matrix):
        records.append(
            _table_row_record(
                cells,
                widths,
                offsets,
                header=row_index == 0,
                rule=row_index < len(matrix) - 1,
                group=f"{group_prefix}-row{row_index}",
            )
        )
    return records


def _is_new_block_start(line: str) -> bool:
    return bool(
        line.startswith("Specimen:")
        or _BULLET_RE.match(line)
        or _ORDERED_RE.match(line)
        or _HEADING_RE.match(line)
        or _RULE_RE.match(line)
        or line.lstrip().startswith(">")
        or _FENCE_RE.match(line)
        or _is_table_row(line)
    )


def _collect_lazy_continuation(
    lines: list[str], index: int, total: int
) -> tuple[list[str], int]:
    """Wrapped source lines that continue a list item without a marker.

    A list item's text commonly wraps across several source lines (the
    design doc does this throughout). Without this, each wrapped line
    became its own paragraph and split the item in two.
    """
    parts: list[str] = []
    while index < total:
        candidate = lines[index].rstrip()
        stripped = candidate.strip()
        if not stripped or _is_new_block_start(candidate):
            break
        parts.append(stripped)
        index += 1
    return parts, index


def _parse_markdown_blocks(text: str) -> list[dict[str, Any]]:
    """A deliberately small, dependency-free markdown block parser.

    Anything not recognised falls through to a paragraph - never a crash.
    Not a full CommonMark implementation; it covers the block types
    mc-vjz5 asked for (headings, paragraphs, bullets/ordered lists with one
    nesting level, fenced/indented code, block quotes, rules, tables).
    """
    blocks: list[dict[str, Any]] = []
    lines = text.expandtabs(4).splitlines()
    total = len(lines)
    index = 0
    paragraph_buffer: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_buffer:
            blocks.append({"type": "paragraph", "text": " ".join(paragraph_buffer)})
            paragraph_buffer.clear()

    while index < total:
        raw_line = lines[index]
        line = raw_line.rstrip()
        stripped = line.strip()

        fence_match = _FENCE_RE.match(line)
        if fence_match:
            flush_paragraph()
            fence_marker = fence_match.group(2)[0] * 3
            code_lines: list[str] = []
            index += 1
            while index < total:
                if lines[index].strip().startswith(fence_marker):
                    index += 1
                    break
                code_lines.append(lines[index])
                index += 1
            blocks.append({"type": "code", "lines": code_lines})
            continue

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if line.startswith("Specimen:"):
            flush_paragraph()
            match = _SPECIMEN_RE.fullmatch(line)
            if match is None:
                raise TodayPageError("A specimen needs a font path and sample text separated by |.")
            blocks.append({"type": "specimen", "path": match.group(1), "text": match.group(2)})
            index += 1
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_paragraph()
            blocks.append(
                {
                    "type": "heading",
                    "level": len(heading_match.group(1)),
                    "text": heading_match.group(2).strip(),
                }
            )
            index += 1
            continue

        if _RULE_RE.match(line):
            flush_paragraph()
            blocks.append({"type": "rule"})
            index += 1
            continue

        if line.lstrip().startswith(">"):
            flush_paragraph()
            quote_parts = []
            while index < total and lines[index].lstrip().startswith(">"):
                quote_match = _QUOTE_RE.match(lines[index])
                quote_parts.append(quote_match.group(1) if quote_match else "")
                index += 1
            blocks.append(
                {"type": "quote", "text": " ".join(part for part in quote_parts if part)}
            )
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            flush_paragraph()
            level = 1 if len(bullet_match.group(1)) >= 2 else 0
            item_text_parts = [bullet_match.group(2).strip()]
            index += 1
            extra, index = _collect_lazy_continuation(lines, index, total)
            item_text_parts.extend(extra)
            blocks.append(
                {
                    "type": "bullet",
                    "ordered": False,
                    "level": level,
                    "text": " ".join(item_text_parts),
                }
            )
            continue

        ordered_match = _ORDERED_RE.match(line)
        if ordered_match:
            flush_paragraph()
            level = 1 if len(ordered_match.group(1)) >= 2 else 0
            item_text_parts = [ordered_match.group(3).strip()]
            index += 1
            extra, index = _collect_lazy_continuation(lines, index, total)
            item_text_parts.extend(extra)
            blocks.append(
                {
                    "type": "bullet",
                    "ordered": True,
                    "level": level,
                    "text": " ".join(item_text_parts),
                }
            )
            continue

        if _is_table_row(line):
            flush_paragraph()
            table_lines = []
            while index < total and lines[index].strip() and _is_table_row(lines[index]):
                table_lines.append(lines[index].strip())
                index += 1
            blocks.append({"type": "table", "rows": table_lines})
            continue

        indent_amount = len(raw_line) - len(raw_line.lstrip(" "))
        previous_type = blocks[-1]["type"] if blocks else None
        if indent_amount >= 4 and not paragraph_buffer and previous_type != "bullet":
            code_lines = []
            while index < total:
                candidate = lines[index]
                if candidate.strip() == "":
                    break
                candidate_indent = len(candidate) - len(candidate.lstrip(" "))
                if candidate_indent < 4:
                    break
                code_lines.append(candidate[4:])
                index += 1
            blocks.append({"type": "code", "lines": code_lines})
            continue

        paragraph_buffer.append(stripped)
        index += 1

    flush_paragraph()
    return blocks


def _markdown_to_line_records(text: str, root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    blocks = _parse_markdown_blocks(text)
    records: list[dict[str, Any]] = []
    ordered_counters: dict[int, int] = {}
    for block_index, block in enumerate(blocks):
        block_type = block["type"]
        if block_type != "bullet":
            ordered_counters = {}

        if block_type == "heading":
            if records:
                records.append(_appendix_blank_record(before_heading=True))
            records.extend(
                _wrap_text_block(block["text"], f"h{block['level']}", indent=0)
            )
        elif block_type == "specimen":
            source = _validate_specimen_path(block["path"], root)
            if records:
                records.append(_appendix_blank_record())
            records.append({
                "style": "specimen",
                "text": block["text"],
                "font_path": block["path"],
                "font_sha256": sha256_file(source),
                "height": APPENDIX_SPECIMEN_HEIGHT,
            })
        elif block_type == "paragraph":
            if records:
                records.append(_appendix_blank_record())
            records.extend(_wrap_text_block(block["text"], "body", indent=0))
        elif block_type == "bullet":
            if records:
                records.append(_appendix_blank_record())
            level = block["level"]
            ordered_counters = {k: v for k, v in ordered_counters.items() if k <= level}
            if block["ordered"]:
                ordered_counters[level] = ordered_counters.get(level, 0) + 1
                marker = f"{ordered_counters[level]}."
            else:
                # WinAnsiEncoding (the base14 fonts' encoding) has no glyph
                # for U+25E6 WHITE BULLET, which reportlab renders as a
                # missing-glyph box - so the nested level uses a supported
                # dash instead of a visually "correct" but unrenderable ring.
                marker = "\u2022" if level == 0 else "-"
            records.extend(
                _wrap_text_block(block["text"], "bullet", indent=level, marker=marker)
            )
        elif block_type == "quote":
            if records:
                records.append(_appendix_blank_record())
            # A bar down the left margin was all a reader saw of the quote's
            # nature (mc-dsb68); the word "Quote:" says it outright, and the
            # indent already sets the passage apart. The label is tied to the
            # first line of the quote so a page break cannot strand it.
            quote_records = [_appendix_record("Quote:", "quote_label", indent=1)]
            quote_records.extend(_wrap_text_block(block["text"], "quote", indent=1))
            group = f"quote{block_index}"
            for record in quote_records[:2]:
                record["group"] = group
            records.extend(quote_records)
        elif block_type == "code":
            if records:
                records.append(_appendix_blank_record())
            for line in block["lines"] or [""]:
                records.extend(_wrap_preformatted_line(line, "code", indent=0))
        elif block_type == "table":
            if records:
                records.append(_appendix_blank_record())
            records.extend(_table_records(block["rows"], f"table{block_index}"))
        elif block_type == "rule":
            if records:
                records.append(_appendix_blank_record())
            records.append(_appendix_record("", "rule", 0))
        else:
            # Unrecognised block: degrade to a body paragraph, never crash.
            text_value = block.get("text") or ""
            if text_value:
                if records:
                    records.append(_appendix_blank_record())
                records.extend(_wrap_text_block(text_value, "body", indent=0))

    if not records:
        records.append(_appendix_record("", "body", 0))
    return records


def _is_heading_style(style: str) -> bool:
    return style in _APPENDIX_STYLE_FONTS and style.startswith("h") and style[1:].isdigit()


def _keep_together_span(records: list[dict[str, Any]], index: int) -> int:
    """How many records from `index` must stay on one page, at least 1.

    A group id marks records that are one visual unit - a table row's
    labelled block, a quote and its label. Splitting one across a page break
    is the "row broken mid-word across pages" failure this exists to stop.
    """
    group = records[index].get("group")
    if not group:
        return 1
    span = index + 1
    while span < len(records) and records[span].get("group") == group:
        span += 1
    return span - index


def _paginate_appendix_lines(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Fill pages by accumulating variable line heights, never orphaning a heading.

    A heading that would be the last thing to fit on a page (with more
    content still to come) is pushed to the next page instead, so it never
    prints alone at the bottom with nothing under it. Records sharing a
    group id move together for the same reason.
    """
    content_top = APPENDIX_CONTENT_TOP
    content_bottom = PAGE_CONTENT_BOTTOM
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    cursor = content_top
    index = 0
    total = len(records)
    while index < total:
        record = records[index]
        if not current and record["style"] == "blank":
            index += 1
            continue
        span = _keep_together_span(records, index)
        if span > 1:
            unit = records[index : index + span]
            unit_height = sum(item["height"] for item in unit)
            if current and cursor - unit_height < content_bottom:
                pages.append(current)
                current = []
                cursor = content_top
                continue
            current.extend(unit)
            cursor -= unit_height
            index += span
            if cursor <= content_bottom:
                pages.append(current)
                current = []
                cursor = content_top
            continue
        height = record["height"]
        if (
            _is_heading_style(record["style"])
            and index + 1 < total
            and current
            and (cursor - height) >= content_bottom
            and (cursor - height - records[index + 1]["height"]) < content_bottom
        ):
            pages.append(current)
            current = []
            cursor = content_top
            continue
        if cursor - height < content_bottom:
            if not current:
                current.append(record)
                pages.append(current)
                current = []
                cursor = content_top
                index += 1
                continue
            pages.append(current)
            current = []
            cursor = content_top
            continue
        current.append(record)
        cursor -= height
        index += 1
    if current:
        pages.append(current)
    if not pages:
        pages.append([])
    return pages


def _draw_box(canvas: Any, bounds: list[float], label: str | None = None) -> None:
    from reportlab.lib.colors import HexColor

    x0, y0, x1, y1 = bounds
    canvas.setLineWidth(1.0)
    canvas.setStrokeColor(HexColor("#111111"))
    canvas.setFillColor(HexColor("#111111"))
    canvas.rect(x0, y0, x1 - x0, y1 - y0, fill=0, stroke=1)
    if label:
        canvas.setFont("Helvetica", 7.0)
        canvas.drawString(x1 + 3.0, (y0 + y1) / 2 - 2.5, label)


# --- v3 document layout (mc-iacbw) --------------------------------------
#
# The v3 document reads in one pass: page 1 says what is NOT in front of the reader
# and why, then each card is followed immediately by the document it is about,
# printed in full. Layout is a flow of measured blocks packed onto pages, and
# a question block (its text, its lettered options, its boxes and its write-in
# line) is ATOMIC: a question can never be separated from the boxes that
# answer it, which was possible under the fixed four-cards-per-page layout.


def _entry_identity(item_id: str, question_key: str) -> str:
    """The stable id for one question, across days and wording edits.

    Per-question state (answered, deferred, discarded) is keyed on this, so it
    must not move when the card is re-rendered, when its neighbours change,
    when the row is printed again tomorrow, or when the question is reworded.

    Both halves have to be wording-independent for that to be true. The
    question key comes from the row, not from the question text. The item id
    comes from the row's own declared key (row_item_id), NOT from its title,
    project and age as it did until 2026-09-01: those are wording, and an
    edited title used to change every entry id under the row and so resurrect
    a question the reader had discarded. decision_number and the printed label are
    presentation only (contract section 2).
    """
    return sha256_bytes(f"{item_id}\0{question_key}".encode("ascii"))[:12]


def _question_set_hash(keys: Iterable[str]) -> str:
    """The fingerprint of a row's COMPLETE question-key set (section 2).

    Always the full set as written in the Active row, never the subset that
    happened to be printed today: questions already answered or discarded are
    left off the page, and hashing what was printed would let a document from
    a day with fewer questions close a row that still has open ones. The
    consumer uses the full-set hash to refuse exactly that.
    """
    return sha256_bytes("\0".join(sorted(keys)).encode("ascii"))


def _box_labels(form: dict[str, Any]) -> list[str]:
    if form["answer_type"] == "yes_no":
        return ["YES", "NO"]
    if form["answer_type"] == "choice":
        return [option["letter"] for option in form["options"]]
    return []


def _printed_box_label(choice: str) -> str:
    if choice == "YES":
        return "Y"
    if choice == "NO":
        return "N"
    if choice == "DEFER":
        return DEFER_LABEL
    if choice == "DISCARD":
        return DISCARD_LABEL
    return choice


def _question_block(
    form: dict[str, Any],
    number: int,
    total: int,
    inner_width: float,
) -> dict[str, Any]:
    """One question, its options, its answer boxes and its write-in line."""
    label = form["question"]
    heading = f"{number}. {label}" if total > 1 else label
    lines = _wrap_text(heading, "Helvetica", BODY_FONT_SIZE, inner_width)
    option_lines: list[str] = []
    for option in form["options"]:
        option_lines.extend(
            _wrap_text(
                f"{option['letter']}. {option['text']}",
                "Helvetica",
                BODY_FONT_SIZE,
                inner_width - 14.0,
            )
        )
    # The question's own recommendation, printed directly under it and its
    # options and above the boxes that answer it, in the question's own style
    # (the reader, 2026-09-14). It is part of the question block, so it can never be
    # separated from the question it advises by a page break.
    recommendation_lines: list[str] = []
    if form.get("recommendation"):
        recommendation_lines = _wrap_text(
            f"Recommended: {form['recommendation']}",
            "Helvetica",
            BODY_FONT_SIZE,
            inner_width,
        )
    writes_in = form["answer_type"] in ("choice", "write_in")
    height = (
        (len(lines) + len(option_lines) + len(recommendation_lines))
        * BODY_LINE_HEIGHT
        + QUESTION_TEXT_GAP
        + BOX_SIZE
        + BOX_LABEL_GAP
        + (WRITE_IN_ROW_HEIGHT if writes_in else 0.0)
        + QUESTION_BLOCK_GAP
    )
    return {
        "kind": "question",
        "height": height,
        "label": label,
        "question_key": form["question_key"],
        "answer_type": form["answer_type"],
        "options": form["options"],
        "decision_number": number,
        "lines": lines,
        "option_lines": option_lines,
        "recommendation": form.get("recommendation"),
        "recommendation_lines": recommendation_lines,
        "box_labels": _box_labels(form),
        "writes_in": writes_in,
    }


def _text_block(kind: str, text: str, font: str, size: float, width: float,
                line_height: float, gap: float) -> dict[str, Any] | None:
    lines = _wrap_text(text, font, size, width)
    if not lines:
        return None
    return {
        "kind": kind,
        "height": len(lines) * line_height + gap,
        "lines": lines,
        "font": font,
        "size": size,
        "line_height": line_height,
    }


def _card_blocks(
    decision: dict[str, Any],
    system_label: str,
    inner_width: float,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if system_label:
        blocks.append(
            {
                "kind": "system",
                "height": SYSTEM_LABEL_FONT_SIZE + SYSTEM_LABEL_GAP + 2.0,
                "lines": [f"System: {system_label}"],
                "font": "Helvetica-Bold",
                "size": SYSTEM_LABEL_FONT_SIZE,
                "line_height": SYSTEM_LABEL_FONT_SIZE + SYSTEM_LABEL_GAP,
            }
        )
    headline = _text_block(
        "headline", decision["question"], "Helvetica-Bold", QUESTION_FONT_SIZE,
        inner_width, QUESTION_LINE_HEIGHT, 3.0,
    )
    if headline:
        blocks.append(headline)
    blocks.append(
        {
            "kind": "dates",
            "height": 14.0,
            "lines": [decision["dates"]],
            "font": "Helvetica",
            "size": 8.0,
            "line_height": 12.0,
        }
    )
    leadin = _text_block(
        "leadin", decision["description"], "Helvetica", BODY_FONT_SIZE,
        inner_width, BODY_LINE_HEIGHT, 6.0,
    )
    if leadin:
        blocks.append(leadin)
    if decision.get("specimen"):
        specimen = decision["specimen"]
        blocks.append({
            "kind": "specimen",
            "height": APPENDIX_SPECIMEN_HEIGHT,
            "text": specimen["text"],
            "font_path": specimen["path"],
            "font_sha256": specimen["sha256"],
        })
    recommendation = _text_block(
        "recommendation", f"Recommendation: {decision['recommendation']}",
        "Helvetica-Bold", BODY_FONT_SIZE, inner_width, BODY_LINE_HEIGHT, 6.0,
    )
    if recommendation:
        blocks.append(recommendation)
    # The "Background, optional: pages N to M." note is NOT a block here: it
    # names absolute page numbers that are only known after this card is
    # paginated (a card that spills onto a second page shifts where its
    # reading actually starts), and it has to repeat on every page of the
    # card, not just wherever one block happens to land. _render_v3_pages
    # draws it from card["read"]["pages"] once pagination is final.
    blocks.append(
        {
            "kind": "heading",
            "height": BODY_LINE_HEIGHT + 6.0,
            "lines": ["Decision"],
            "font": "Helvetica-Bold",
            "size": BODY_FONT_SIZE,
            "line_height": BODY_LINE_HEIGHT,
        }
    )
    forms = decision["decision_forms"]
    for index, form in enumerate(forms, 1):
        blocks.append(_question_block(form, index, len(forms), inner_width))
    return blocks


def _pack_blocks(
    blocks: list[dict[str, Any]],
    start_page: int,
) -> list[dict[str, Any]]:
    """Lay measured blocks onto as many pages as they need, in order.

    Returns one fragment per page: its top, its bottom, and the blocks on it
    with an absolute y for each. A block taller than a whole page is a build
    failure, not something to clip: a clipped question is a question the reader
    never sees.
    """
    usable = PAGE_CONTENT_TOP - PAGE_CONTENT_BOTTOM - 2 * CARD_PADDING
    fragments: list[dict[str, Any]] = []
    page = start_page
    top = PAGE_CONTENT_TOP
    cursor = top - CARD_PADDING
    placed: list[dict[str, Any]] = []
    for block in blocks:
        if block["height"] > usable:
            raise TodayPageError(
                "One question or paragraph is taller than a whole page and "
                "would have to be cut in half to fit. No document was written."
            )
        if cursor - block["height"] < PAGE_CONTENT_BOTTOM + CARD_PADDING:
            fragments.append(
                {
                    "page": page,
                    "x": PAGE_MARGIN_X,
                    "width": PAGE_W - 2 * PAGE_MARGIN_X,
                    "top": round(top, 2),
                    "bottom": round(cursor - CARD_PADDING, 2),
                    "blocks": placed,
                }
            )
            page += 1
            top = PAGE_CONTENT_TOP
            cursor = top - CARD_PADDING
            placed = []
        block = dict(block)
        block["y_top"] = round(cursor, 2)
        placed.append(block)
        cursor -= block["height"]
    fragments.append(
        {
            "page": page,
            "x": PAGE_MARGIN_X,
            "width": PAGE_W - 2 * PAGE_MARGIN_X,
            "top": round(top, 2),
            "bottom": round(cursor - CARD_PADDING, 2),
            "blocks": placed,
        }
    )
    return fragments


def _place_question_geometry(
    block: dict[str, Any],
    card_x: float,
    card_width: float,
) -> None:
    """Give one packed question block its absolute box and line geometry."""
    text_top = block["y_top"]
    text_lines = (
        len(block["lines"])
        + len(block["option_lines"])
        + len(block.get("recommendation_lines") or [])
    )
    box_top = text_top - text_lines * BODY_LINE_HEIGHT - QUESTION_TEXT_GAP
    center_y = box_top - BOX_SIZE / 2
    first_x = card_x + CARD_PADDING + 12.0 + BOX_SIZE / 2
    boxes: list[dict[str, Any]] = []
    for index, label in enumerate(block["box_labels"]):
        boxes.append(
            {"choice": label, "bbox": box(first_x + index * ANSWER_BOX_PITCH, center_y)}
        )
    tail_x = (
        first_x
        + len(block["box_labels"]) * ANSWER_BOX_PITCH
        + ANSWER_GROUP_GAP
    )
    boxes.append({"choice": "DEFER", "bbox": box(tail_x, center_y)})
    boxes.append(
        {"choice": "DISCARD", "bbox": box(tail_x + ANSWER_BOX_PITCH, center_y)}
    )
    block["boxes"] = boxes
    block["box_center_y"] = round(center_y, 2)
    block["label_y"] = round(box_top - BOX_SIZE - BOX_LABEL_GAP + 2.0, 2)
    if block["writes_in"]:
        row_top = box_top - BOX_SIZE - BOX_LABEL_GAP
        x0 = card_x + CARD_PADDING + 66.0
        x1 = card_x + card_width - CARD_PADDING
        first_line_y = row_top - WRITE_IN_TOP_GAP
        lines_y = [
            first_line_y - index * WRITE_IN_LINE_GAP
            for index in range(WRITE_IN_LINE_COUNT)
        ]
        block["write_in_lines"] = [
            [round(x0, 2), round(y, 2), round(x1, 2), round(y, 2)] for y in lines_y
        ]
        # The first ruled line doubles as the anchor for the "Or write in:"
        # label at draw time, so it is kept under the old singular key too.
        block["write_in_line"] = block["write_in_lines"][0]
        last_line_y = lines_y[-1]
        # One bbox spans all three ruled lines (top line's headroom to bottom
        # line's undershoot), because the consumer (decision_resolver_v3.py,
        # tp._inside against entry["write_in"]["bbox"]) and the PNG cropper
        # (render_write_in_pngs) both read a single rectangle per entry, not
        # per line. Enlarging this bbox is what makes the extractor read the
        # whole taller area rather than just the old single line.
        block["write_in"] = {
            "bbox": [
                round(x0 - 4.0, 2),
                round(last_line_y - 10.0, 2),
                round(x1 + 2.0, 2),
                round(first_line_y + 14.0, 2),
            ]
        }
    else:
        block["write_in_line"] = None
        block["write_in_lines"] = []
        block["write_in"] = None


# The cover names every row that was held off the document. It used to print
# the row's whole title, and a title is free prose that no freshness check ever
# reads, because a held row never renders a card. On 2026-09-13 that put "three
# builds and their manifests remain on the Mac Studio" on the cover while the
# card three pages later said eight archives and 11 GB, both in the same
# document. The cover now prints the row's SUBJECT and the reason it was held,
# and nothing else: a held row is a pointer, not a place to restate facts
# (mc-9jd4t).
COVER_HELD_LABEL_WORDS = 12


def _cover_held_label(title: str) -> str:
    """The short subject of a held row, with its fact-bearing tail dropped."""
    label = clean_markdown(title).strip()
    label = re.sub(r"\s*\([^()]*\)\s*$", "", label).strip()
    head = label.split(":", 1)[0].strip()
    if len(head.split()) >= 3:
        label = head
    words = label.split()
    if len(words) > COVER_HELD_LABEL_WORDS:
        label = " ".join(words[:COVER_HELD_LABEL_WORDS])
    return label.rstrip(" ,;-")


# The cover tells the reader why an item is not in front of him. The reasons the
# verifier writes are for whoever fixes the row: they name the card contract,
# the Ask, the Recommendation and Decide sections. None of that means anything
# to him (the cross-reviewer, 2026-09-13, mc-9jd4t). Every held reason is translated here
# into one plain sentence, and the raw text never reaches the page - which is
# why no internal term can appear on a cover, whatever the verifier writes.
COVER_HELD_PREFIX = "Not ready for a decision yet"
COVER_HELD_REASONS = (
    (
        "each question needs its own recommendation",
        "each of its questions still needs its own recommendation",
    ),
    ("card contract:", "it needs rewriting before it can be asked"),
    ("is closed", "it points at work that is already finished"),
    ("is unknown to the tracker", "it points at a ticket that does not exist"),
    ("referenced file missing", "a document it points at is missing"),
    ("could not be read", "a document it points at could not be read"),
)


def _plain_held_reason(reason: Any) -> str:
    """One plain sentence for why a held item is not in this document.

    Total by construction: an unrecognised reason falls back to the bare
    sentence rather than passing the verifier's wording through.
    """
    text = str(reason or "")
    for marker, plain in COVER_HELD_REASONS:
        if marker in text:
            return f"{COVER_HELD_PREFIX}, {plain}."
    return f"{COVER_HELD_PREFIX}."


def _cover_blocks(
    held: list[dict[str, Any]],
    card_count: int,
    inner_width: float,
    cover_note: str | None = None,
) -> list[dict[str, Any]]:
    """Page 1: what is not in this document, and why (contract section 5).

    A supplement passes cover_note to say, in its own words and before
    anything else, how it relates to the document the reader already has. Without
    that sentence a second document with its own cards reads as a replacement
    for the first one.
    """
    blocks: list[dict[str, Any]] = []
    if cover_note:
        note = _text_block(
            "cover_body", cover_note, "Helvetica-Bold", BODY_FONT_SIZE,
            inner_width, BODY_LINE_HEIGHT, COVER_HEADING_GAP,
        )
        if note:
            note["height"] += COVER_HEADING_GAP
            blocks.append(note)
    blocks.append(
        {
            "kind": "cover_heading",
            "height": 26.0,
            "lines": ["What is not in this document"],
            "font": "Helvetica-Bold",
            "size": 14.0,
            "line_height": 18.0,
        }
    )
    if held:
        intro = (
            "These items were left out of today's questions for the reasons "
            "below. Parked items remain in the review queue."
        )
    else:
        intro = (
            "Only questions selected for today appear here. "
            "Parked items remain in the review queue."
        )
    body = _text_block(
        "cover_body", intro, "Helvetica", BODY_FONT_SIZE, inner_width,
        BODY_LINE_HEIGHT, COVER_HEADING_GAP,
    )
    if body:
        blocks.append(body)
    for item in held:
        lines = _wrap_text(
            f"{_cover_held_label(item['title'])} - "
            f"{_plain_held_reason(item.get('reason'))}",
            "Helvetica", BODY_FONT_SIZE, inner_width - 14.0,
        )
        blocks.append(
            {
                "kind": "cover_item",
                "height": len(lines) * BODY_LINE_HEIGHT + 6.0,
                "lines": lines,
                "font": "Helvetica",
                "size": BODY_FONT_SIZE,
                "line_height": BODY_LINE_HEIGHT,
                "marker": "-",
            }
        )
    count_line = (
        "There is 1 card to answer after this page."
        if card_count == 1
        else f"There are {card_count} cards to answer after this page."
    )
    count = _text_block(
        "cover_count", count_line, "Helvetica-Bold", BODY_FONT_SIZE,
        inner_width, BODY_LINE_HEIGHT, COVER_HEADING_GAP,
    )
    if count:
        count["height"] += COVER_HEADING_GAP
        blocks.append(count)
    how = _text_block(
        "cover_body",
        "Every question below carries a Defer box (ask me again tomorrow, "
        "unchanged) and a Discard box (never ask me this again). Tick one box "
        "per question. Where there is a line, write on it.",
        "Helvetica", BODY_FONT_SIZE, inner_width, BODY_LINE_HEIGHT, 6.0,
    )
    if how:
        blocks.append(how)
    return blocks


def _assert_box_separation(entries: list[dict[str, Any]]) -> None:
    """No two answer boxes on a page may share a hit region.

    Every box is matched against ink after being grown by BOX_TOLERANCE on
    each side. If two grown boxes overlap, one tick can be read as two
    answers, which is how the 2026-09-01 sheet produced two false YES marks.
    Adding Defer, Discard and up to five lettered choices multiplies the boxes
    on a page, so this is checked on the real geometry of every document
    rather than trusted to the pitch constants.
    """
    per_page: dict[int, list[tuple[str, str, list[float]]]] = {}
    for entry in entries:
        for entry_box in entry["boxes"]:
            per_page.setdefault(entry["page"], []).append(
                (entry["entry_id"], entry_box["choice"], _expanded(entry_box["bbox"]))
            )
    for page, boxes in per_page.items():
        for first in range(len(boxes)):
            for second in range(first + 1, len(boxes)):
                if _intersects(boxes[first][2], boxes[second][2]):
                    raise TodayPageError(
                        "Two answer boxes on page "
                        f"{page} are close enough that one tick could be read "
                        f"as two answers ({boxes[first][0]}/{boxes[first][1]} "
                        f"and {boxes[second][0]}/{boxes[second][1]}). "
                        "No document was written."
                    )


def _assert_v3_bounds(
    entries: list[dict[str, Any]],
    fragments_by_page: dict[int, dict[str, Any]],
) -> None:
    for entry in entries:
        fragment = fragments_by_page.get(entry["page"])
        for entry_box in entry["boxes"]:
            x0, y0, x1, y1 = _expanded(entry_box["bbox"])
            if x0 < 0 or y0 < PAGE_CONTENT_BOTTOM - BOX_TOLERANCE or x1 > PAGE_W or y1 > PAGE_H:
                raise TodayPageError(
                    f"Answer box {entry['entry_id']}/{entry_box['choice']} "
                    "would print off the page. No document was written."
                )
            if fragment is None:
                continue
            if (
                x0 < fragment["x"]
                or x1 > fragment["x"] + fragment["width"]
                or y0 < fragment["bottom"]
                or y1 > fragment["top"]
            ):
                raise TodayPageError(
                    f"Answer box {entry['entry_id']}/{entry_box['choice']} "
                    "would print outside its own card. No document was written."
                )


def _action_blocks(
    actions: list[dict[str, Any]],
    inner_width: float,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        lines = _wrap_text(
            action["label"], "Helvetica-Bold", BODY_FONT_SIZE, inner_width - 40.0
        )
        blocks.append(
            {
                "kind": "action",
                "height": max(46.0, len(lines) * BODY_LINE_HEIGHT + 2 * CARD_PADDING + 8.0),
                "lines": lines,
                "label": action["label"],
                "action_index": index,
                "action": {"type": action["type"], "payload": action["payload"]},
            }
        )
    return blocks


def _prepare_reads(
    decisions: list[dict[str, Any]],
    read_root: Path,
) -> list[dict[str, Any]]:
    """Paginate each card's Read document into styled pages, in full."""
    reads: list[dict[str, Any] | None] = []
    for decision in decisions:
        if not decision.get("read"):
            # A legacy row names no reading document. Current rows cannot get
            # here: the parser holds them (require_keys=True).
            reads.append(None)
            continue
        path = decision["read"]["path"]
        source = _validate_read_path(path, read_root)
        text = _transliterate_typography(source.read_text(encoding="utf-8"))
        pages = _paginate_appendix_lines(
            _appendix_as_of_records(source) + _markdown_to_line_records(text, read_root)
        )
        reads.append(
            {
                "path": path,
                "sha256": sha256_bytes(text.encode("utf-8")),
                "label": Path(path).name,
                "pages": pages,
            }
        )
    return reads


def _layout_v3_document(
    actions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    reads: list[dict[str, Any]],
    held: list[dict[str, Any]],
    cover_note: str | None = None,
) -> dict[str, Any]:
    """Lay out the whole document: cover, then card + its reading, per card."""
    inner_width = PAGE_W - 2 * PAGE_MARGIN_X - 2 * CARD_PADDING
    cover = _pack_blocks(
        _cover_blocks(held, len(decisions), inner_width, cover_note), 1
    )
    page = cover[-1]["page"] + 1

    entries: list[dict[str, Any]] = []
    action_fragments: list[dict[str, Any]] = []
    if actions:
        action_fragments = _pack_blocks(_action_blocks(actions, inner_width), page)
        for fragment in action_fragments:
            for block in fragment["blocks"]:
                center_y = block["y_top"] - block["height"] / 2
                block["box"] = box(PAGE_MARGIN_X + 30.0, center_y)
                entries.append(
                    {
                        "entry_id": f"action-{block['action_index'] + 1:02d}",
                        "kind": "action",
                        "label": block["label"],
                        "page": fragment["page"],
                        "boxes": [{"choice": "RUN", "bbox": block["box"]}],
                        "action": block["action"],
                        "layout": {
                            "page": fragment["page"],
                            "x": fragment["x"],
                            "width": fragment["width"],
                            "top": round(block["y_top"], 2),
                            "bottom": round(block["y_top"] - block["height"], 2),
                            "center_y": round(center_y, 2),
                            "label_lines": block["lines"],
                        },
                    }
                )
        page = action_fragments[-1]["page"] + 1

    cards: list[dict[str, Any]] = []
    appendices: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, decision in enumerate(decisions):
        read = reads[index]
        card_id = f"card-{index + 1:03d}"
        system_label = card_system_label(decision)
        # The row's whole key set, even when state left some questions off
        # this document (contract section 2, corrected 2026-09-01).
        row_keys = decision.get("all_question_keys") or [
            form["question_key"] for form in decision["decision_forms"]
        ]
        fragments = _pack_blocks(
            _card_blocks(decision, system_label, inner_width),
            page,
        )
        entry_ids: list[str] = []
        for fragment in fragments:
            for block in fragment["blocks"]:
                if block["kind"] != "question":
                    continue
                _place_question_geometry(block, fragment["x"], fragment["width"])
                entry_id = _entry_identity(
                    decision["item_id"], block["question_key"]
                )
                if entry_id in seen_ids:
                    raise TodayPageError(
                        "Two questions on this document share a question key, "
                        "so their answers could not be told apart "
                        f"({block['question_key']!r}). No document was written."
                    )
                seen_ids.add(entry_id)
                block["entry_id"] = entry_id
                entry_ids.append(entry_id)
                entries.append(
                    {
                        "entry_id": entry_id,
                        "card_id": card_id,
                        "kind": "decision",
                        "label": block["label"],
                        "question_key": block["question_key"],
                        "item_id": decision["item_id"],
                        "decision_number": block["decision_number"],
                        "page": fragment["page"],
                        "answer_type": block["answer_type"],
                        "options": block["options"],
                        "boxes": block["boxes"],
                        "write_in": block["write_in"],
                    }
                )
        card_pages = [fragment["page"] for fragment in fragments]
        read_first = card_pages[-1] + 1
        read_pages = (
            list(range(read_first, read_first + len(read["pages"])))
            if read
            else []
        )
        page = (read_pages[-1] if read_pages else card_pages[-1]) + 1
        cards.append(
            {
                "card_id": card_id,
                "item_id": decision["item_id"],
                # The row key itself, so a consumer can find the Active row by
                # key rather than by title when it cross-checks closure. The
                # item id is a hash of this and cannot be searched for in the
                # queue. None only for a legacy row, which declares no key.
                "row_key": decision.get("row_key"),
                "system": system_label,
                "headline": decision["question"],
                "dates": decision["dates"],
                "ask": decision["ask"],
                "description": decision["description"],
                "recommendation": decision["recommendation"],
                "page": card_pages[0],
                "page_numbers": card_pages,
                "decision_entry_ids": entry_ids,
                "fact_check": decision.get("fact_check"),
                "question_keys": list(row_keys),
                "printed_question_keys": [
                    form["question_key"] for form in decision["decision_forms"]
                ],
                "question_set_hash": _question_set_hash(row_keys),
                "read": (
                    {
                        "path": read["path"],
                        "sha256": read["sha256"],
                        "pages": (
                            [read_pages[0], read_pages[-1]] if read_pages else []
                        ),
                    }
                    if read
                    else None
                ),
                "fragments": fragments,
                "layout": fragments[0],
            }
        )
        if read is None:
            continue
        appendices.append(
            {
                "appendix": _appendix_letter(len(appendices)),
                "label": read["label"],
                "path": read["path"],
                "status": "available",
                "sha256": read["sha256"],
                "card_id": card_id,
                "card_index": index,
                "start_page": read_pages[0] if read_pages else 0,
                "page_numbers": read_pages,
                "pages": read["pages"],
            }
        )

    fragments_by_page = {
        fragment["page"]: fragment
        for group in [cover, action_fragments] + [card["fragments"] for card in cards]
        for fragment in group
    }
    _assert_v3_bounds(entries, fragments_by_page)
    _assert_box_separation(entries)
    return {
        "cover": cover,
        "action_fragments": action_fragments,
        "entries": entries,
        "cards": cards,
        "appendices": appendices,
        "page_count": page - 1,
        "cover_pages": [fragment["page"] for fragment in cover],
        "answerable_pages": sorted(
            {fragment["page"] for fragment in action_fragments}
            | {number for card in cards for number in card["page_numbers"]}
        ),
    }


def build_manifest(
    date: str,
    actions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    source_hashes: dict[str, str],
    verification: dict[str, str] | None = None,
    attachment_root: Path = REPO_ROOT,
    excluded_already_answered: list[dict[str, Any]] | None = None,
    held: list[dict[str, Any]] | None = None,
    settled: list[dict[str, Any]] | None = None,
    require_keys: bool = True,
    document_name: str | None = None,
    cover_note: str | None = None,
) -> dict[str, Any]:
    # require_keys=False renders a LEGACY row (no question keys, no answer
    # forms, no reading document). Archived-ink replay rebuilds manifests that
    # way; nothing printed for the reader today may use it.
    # A decision that has already been through the parser keeps the shape it
    # arrived in. Re-normalising here would re-read the Ask and put back every
    # question the state filter had just left off, which is how a question
    # The reader discarded would reappear on the page (found 2026-09-01 testing the
    # filter). Raw rows - the shape most tests and callers pass - normalise as
    # before.
    normalized_decisions = [
        decision
        if decision.get("decision_forms") is not None
        else _decision_copy(decision, attachment_root, require_keys=require_keys)
        for decision in decisions
    ]
    excluded_already_answered = list(excluded_already_answered or [])
    held = list(held or [])
    settled = list(settled or [])
    verification = verification or {
        "verified_at": "2026-07-28T08:00:00-07:00",
        "verified_at_human": "July 28 at 8:00 AM",
    }
    reads = _prepare_reads(normalized_decisions, attachment_root)
    document = _layout_v3_document(
        actions, normalized_decisions, reads, held, cover_note
    )
    # A second document for the same day (a supplement carrying cards the
    # main document is missing) needs its own name and its own page id, or
    # its ink would resolve against the main document's manifest and its
    # registry entry would collide with it.
    document_name = document_name or f"decisions-{date}"

    # The geometry is part of the page's identity. page_id used to be derived
    # from content alone, so the same queue rendered at a different box size
    # produced the SAME page_id - and ink drawn on the old sheet would have
    # validated against the new manifest and been read against boxes that had
    # moved. Naming the geometry here makes that impossible (mc-rrjv).
    geometry = {
        "box_size": BOX_SIZE,
        "box_tolerance": BOX_TOLERANCE,
        "min_row_pitch": MIN_ROW_PITCH,
        "answer_box_pitch": ANSWER_BOX_PITCH,
        "answer_group_gap": ANSWER_GROUP_GAP,
    }
    seed = {
        "schema_version": SCHEMA_VERSION,
        "layout_version": LAYOUT_VERSION,
        "geometry": geometry,
        "date": date,
        "actions": actions,
        "decisions": normalized_decisions,
        "source_hashes": source_hashes,
        "verification": verification,
        "appendices": document["appendices"],
        "held": held,
        "document_name": document_name,
        "cover_note": cover_note or "",
    }
    page_id = f"{document_name}-{sha256_bytes(canonical_json(seed).encode())[:12]}"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "layout_version": LAYOUT_VERSION,
        "page_id": page_id,
        "date": date,
        "document_name": document_name,
        "page_count": document["page_count"],
        "answerable_page_count": len(document["answerable_pages"]),
        "answerable_pages": document["answerable_pages"],
        "cover_pages": document["cover_pages"],
        "cover": document["cover"],
        "action_fragments": document["action_fragments"],
        "page_size": {"width": PAGE_W, "height": PAGE_H, "units": "pdf_points"},
        "geometry": geometry,
        "source_hashes": source_hashes,
        "verification": verification,
        "entry_counts": {
            "actions": len(actions),
            "cards": len(document["cards"]),
            "decisions": sum(
                len(decision["decision_questions"])
                for decision in normalized_decisions
            ),
            "excluded_already_answered": len(excluded_already_answered),
            "held": len(held),
            "settled": len(settled),
        },
        "excluded_already_answered": excluded_already_answered,
        "held": held,
        # Rows whose every question is already answered or discarded. NOT a
        # hold: nothing is being withheld from the reader, the row is finished and
        # should already have left Active. Recorded so a finished row that is
        # still sitting in Active is visible somewhere.
        "settled": settled,
        "entries": document["entries"],
        "cards": document["cards"],
        "appendices": document["appendices"],
    }
    manifest["manifest_hash"] = manifest_digest(manifest)
    return manifest


def _draw_appendix_table_row(pdf: Any, record: dict[str, Any], text_y: float) -> float:
    """Draw one table row: each column's wrapped lines at its own x, then the
    rule that separates it from the next row."""
    from reportlab.lib.colors import HexColor

    style = record.get("style", "table")
    font_name, font_size = _APPENDIX_STYLE_FONTS.get(
        style, ("Helvetica", APPENDIX_FONT_SIZE)
    )
    line_height = record.get("line_height", APPENDIX_TABLE_LINE_HEIGHT)
    height = record.get("height", line_height)
    base_x = PAGE_MARGIN_X + record.get("indent", 0) * APPENDIX_INDENT_UNIT
    columns = record.get("columns") or []
    pdf.setFillColor(HexColor("#111111"))
    pdf.setFont(font_name, font_size)
    for index, cell_lines in enumerate(record.get("cells") or []):
        column = columns[index] if index < len(columns) else {}
        cell_x = base_x + column.get("x", 0.0)
        cell_y = text_y
        for cell_line in cell_lines:
            if cell_line:
                pdf.drawString(cell_x, cell_y, cell_line)
            cell_y -= line_height
    bottom = text_y - height
    if record.get("rule") and columns:
        last = columns[-1]
        right = min(
            base_x + last.get("x", 0.0) + last.get("width", 0.0),
            PAGE_W - PAGE_MARGIN_X,
        )
        pdf.setStrokeColor(HexColor("#bbbbbb"))
        pdf.setLineWidth(0.5)
        # `bottom` is the NEXT row's first baseline, so a rule sitting just
        # above `bottom` strikes through that row's text. One line height up
        # puts it in the white band: clear of this row's descenders and clear
        # of the next row's capitals.
        rule_y = bottom + APPENDIX_TABLE_LINE_HEIGHT - 1.0
        pdf.line(base_x, rule_y, right, rule_y)
    return bottom


def _draw_appendix_line(
    pdf: Any, line: Any, text_y: float,
    specimen_x: float | None = None, specimen_width: float | None = None,
) -> float:
    """Draw one appendix line record and return the next line's y.

    Accepts both the current styled-record shape and the pre-mc-vjz5 bare
    string shape (archived manifests under thoughts/remarkable-archive/ carry
    the old shape), so a re-render of an old manifest degrades to plain
    monospace text instead of crashing.
    """
    from reportlab.lib.colors import HexColor

    if isinstance(line, str):
        pdf.setFillColor(HexColor("#111111"))
        pdf.setFont("Courier", APPENDIX_FONT_SIZE)
        pdf.drawString(PAGE_MARGIN_X, text_y, line)
        return text_y - APPENDIX_LINE_HEIGHT

    style = line.get("style", "body")
    height = line.get("height", APPENDIX_LINE_HEIGHT)
    indent = line.get("indent", 0)
    marker = line.get("marker")
    text = line.get("text", "")
    base_x = (
        (specimen_x if specimen_x is not None else PAGE_MARGIN_X)
        + indent * APPENDIX_INDENT_UNIT
    )

    if line.get("cells") is not None:
        return _draw_appendix_table_row(pdf, line, text_y)
    if style == "blank":
        return text_y - height
    if style == "rule":
        pdf.setStrokeColor(HexColor("#999999"))
        pdf.setLineWidth(0.75)
        rule_y = text_y - height / 2
        pdf.line(PAGE_MARGIN_X, rule_y, PAGE_W - PAGE_MARGIN_X, rule_y)
        return text_y - height
    if style == "specimen":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        source = _validate_specimen_path(line["font_path"], REPO_ROOT)
        if sha256_file(source) != line["font_sha256"]:
            raise TodayPageError(f"The specimen font changed after the page was prepared: {line['font_path']}.")
        font_name = "Specimen_" + line["font_sha256"][:16]
        try:
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(source)))
            available_width = (
                specimen_width
                if specimen_width is not None else PAGE_W - 2 * PAGE_MARGIN_X
            )
            if pdfmetrics.stringWidth(text, font_name, APPENDIX_SPECIMEN_SIZE) > available_width:
                if specimen_width is None:
                    raise TodayPageError("The specimen text is too wide for the appendix page.")
                raise TodayPageError("The specimen text is too wide for its card.")
        except (OSError, ValueError) as exc:
            raise TodayPageError(f"The specimen font could not be rendered: {line['font_path']}.") from exc
        pdf.setFillColor(HexColor("#111111"))
        pdf.setFont(font_name, APPENDIX_SPECIMEN_SIZE)
        pdf.drawString(base_x, text_y - 5.0, text)
        pdf.setFont("Helvetica", APPENDIX_FONT_SIZE)
        pdf.drawString(base_x, text_y - 27.0, f"Font: {source.stem}")
        return text_y - height

    font_name, font_size = _APPENDIX_STYLE_FONTS.get(style, ("Helvetica", APPENDIX_FONT_SIZE))
    pdf.setFillColor(HexColor("#111111"))
    pdf.setFont(font_name, font_size)
    text_x = base_x + APPENDIX_MARKER_GAP if style in _APPENDIX_HANGING_STYLES else base_x
    if marker:
        pdf.drawString(base_x, text_y, marker)
    if text:
        pdf.drawString(text_x, text_y, text)
    return text_y - height


def _draw_labelled_box(pdf: Any, bounds: list[float], label: str, label_y: float) -> None:
    """One answer box with its name printed under it, centred.

    The label goes under the box, not beside it: "Discard" beside a box at a
    48-point pitch runs into the next box, and a box whose name is ambiguous
    is worse than no box at all.
    """
    from reportlab.lib.colors import HexColor

    x0, y0, x1, y1 = bounds
    pdf.setLineWidth(1.0)
    pdf.setStrokeColor(HexColor("#111111"))
    pdf.setFillColor(HexColor("#111111"))
    pdf.rect(x0, y0, x1 - x0, y1 - y0, fill=0, stroke=1)
    pdf.setFont("Helvetica", BOX_LABEL_FONT_SIZE)
    pdf.setFillColor(HexColor("#333333"))
    pdf.drawCentredString((x0 + x1) / 2, label_y, label)


def _draw_text_block(pdf: Any, block: dict[str, Any], x: float) -> None:
    from reportlab.lib.colors import HexColor

    colors = {
        "dates": "#666666",
        "system": "#666666",
        "cover_body": "#333333",
    }
    pdf.setFillColor(HexColor(colors.get(block["kind"], "#111111")))
    pdf.setFont(block["font"], block["size"])
    text_x = x + (14.0 if block.get("marker") else 0.0)
    text_y = block["y_top"] - block["size"]
    if block.get("marker"):
        pdf.drawString(x, text_y, block["marker"])
    for line in block["lines"]:
        pdf.drawString(text_x, text_y, line)
        text_y -= block["line_height"]


def _draw_question_block(pdf: Any, block: dict[str, Any], x: float) -> None:
    from reportlab.lib.colors import HexColor

    pdf.setFillColor(HexColor("#111111"))
    pdf.setFont("Helvetica", BODY_FONT_SIZE)
    text_y = block["y_top"] - BODY_FONT_SIZE
    for line in block["lines"]:
        pdf.drawString(x, text_y, line)
        text_y -= BODY_LINE_HEIGHT
    for line in block["option_lines"]:
        pdf.drawString(x + 14.0, text_y, line)
        text_y -= BODY_LINE_HEIGHT
    for line in block.get("recommendation_lines") or []:
        pdf.drawString(x, text_y, line)
        text_y -= BODY_LINE_HEIGHT
    for entry_box in block["boxes"]:
        _draw_labelled_box(
            pdf,
            entry_box["bbox"],
            _printed_box_label(entry_box["choice"]),
            block["label_y"],
        )
    if block["write_in_line"]:
        x0, y0, x1, _ = block["write_in_line"]
        pdf.setFillColor(HexColor("#555555"))
        pdf.setFont("Helvetica", 8.0)
        pdf.drawString(x, y0 - 1.0, WRITE_IN_LABEL)
        pdf.setStrokeColor(HexColor("#777777"))
        pdf.setLineWidth(0.5)
        for line in block["write_in_lines"]:
            pdf.line(*line)


def _draw_fragment(pdf: Any, fragment: dict[str, Any], outline: bool) -> None:
    from reportlab.lib.colors import HexColor

    if outline:
        pdf.setStrokeColor(HexColor("#BBBBBB"))
        pdf.setLineWidth(0.7)
        pdf.roundRect(
            fragment["x"],
            fragment["bottom"],
            fragment["width"],
            fragment["top"] - fragment["bottom"],
            6,
            fill=0,
            stroke=1,
        )
    text_x = fragment["x"] + CARD_PADDING
    for block in fragment["blocks"]:
        if block["kind"] == "question":
            _draw_question_block(pdf, block, text_x)
        elif block["kind"] == "specimen":
            _draw_appendix_line(
                pdf,
                {**block, "style": "specimen"},
                block["y_top"] - (APPENDIX_SPECIMEN_SIZE - 4.0),
                specimen_x=text_x,
                specimen_width=fragment["width"] - 2 * CARD_PADDING,
            )
        elif block["kind"] == "action":
            _draw_labelled_box(
                pdf, block["box"], "RUN", block["box"][1] - BOX_LABEL_GAP
            )
            pdf.setFillColor(HexColor("#111111"))
            pdf.setFont("Helvetica-Bold", BODY_FONT_SIZE)
            text_y = block["y_top"] - CARD_PADDING - BODY_FONT_SIZE
            for line in block["lines"]:
                pdf.drawString(text_x + 46.0, text_y, line)
                text_y -= BODY_LINE_HEIGHT
        else:
            _draw_text_block(pdf, block, text_x)


def _render_v3_pages(pdf: Any, manifest: dict[str, Any]) -> None:
    """Draw the v3 document: the cover, then each card and its reading."""
    from reportlab.lib.colors import HexColor

    fragments_by_page: dict[int, tuple[dict[str, Any], bool]] = {}
    for fragment in manifest.get("cover") or []:
        fragments_by_page[fragment["page"]] = (fragment, False)
    for fragment in manifest.get("action_fragments") or []:
        fragments_by_page[fragment["page"]] = (fragment, True)
    headline_by_page: dict[int, str] = {}
    card_by_page: dict[int, dict[str, Any]] = {}
    for card in manifest["cards"]:
        for fragment in card["fragments"]:
            fragments_by_page[fragment["page"]] = (fragment, True)
            headline_by_page[fragment["page"]] = card["headline"]
            card_by_page[fragment["page"]] = card
    reading_by_page: dict[int, tuple[dict[str, Any], int, list[Any]]] = {}
    reading_headline: dict[str, str] = {
        card["card_id"]: card["headline"] for card in manifest["cards"]
    }
    for appendix in manifest["appendices"]:
        for index, lines in enumerate(appendix["pages"]):
            reading_by_page[appendix["page_numbers"][index]] = (
                appendix,
                index,
                lines,
            )

    for page in range(1, manifest["page_count"] + 1):
        pdf.setFillColor(HexColor("#111111"))
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(PAGE_MARGIN_X, PAGE_H - 32, f"Decisions - {manifest['date']}")
        pdf.setFont("Helvetica", 9)
        pdf.drawRightString(
            PAGE_W - PAGE_MARGIN_X,
            PAGE_H - 30,
            f"Page {page} of {manifest['page_count']}",
        )
        pdf.setFillColor(HexColor("#555555"))
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(
            PAGE_MARGIN_X,
            PAGE_H - 46,
            "Rendered "
            f"{manifest['verification']['verified_at_human']}. "
            "Facts are dated where they were not re-checked.",
        )

        reading = reading_by_page.get(page)
        if reading:
            appendix, page_index, lines = reading
            headline = reading_headline.get(appendix.get("card_id", ""), "")
            pdf.setFillColor(HexColor("#111111"))
            pdf.setFont("Helvetica-Bold", 11)
            continuation = " (continued)" if page_index else ""
            # A long card headline has to wrap, not run off the right edge.
            header_lines = _wrap_text(
                f"Reading for: {headline}{continuation}",
                "Helvetica-Bold",
                11.0,
                PAGE_W - 2 * PAGE_MARGIN_X,
            )[:2]
            header_y = PAGE_H - 68
            for line in header_lines:
                pdf.drawString(PAGE_MARGIN_X, header_y, line)
                header_y -= 13.0
            pdf.setFillColor(HexColor("#777777"))
            pdf.setFont("Helvetica", 8.0)
            # Never print the repository path here (mc-iacbw): the reader reads
            # only the page position within the reading, not where the file
            # lives on disk. The path stays in the manifest.
            pdf.drawString(
                PAGE_MARGIN_X,
                header_y - 1.0,
                f"page {page_index + 1} of {len(appendix['pages'])} of the "
                "reading",
            )
            text_y = min(APPENDIX_CONTENT_TOP - 6.0, header_y - 18.0)
            for line in lines:
                text_y = _draw_appendix_line(pdf, line, text_y)
        else:
            fragment, outline = fragments_by_page[page]
            headline = headline_by_page.get(page)
            note_y = PAGE_H - 60
            if outline and headline and fragment["blocks"] and fragment[
                "blocks"
            ][0]["kind"] == "question":
                pdf.setFillColor(HexColor("#666666"))
                pdf.setFont("Helvetica-Oblique", 8.5)
                pdf.drawString(
                    PAGE_MARGIN_X, note_y, f"Continued: {headline}"
                )
                note_y -= 13.0
            card = card_by_page.get(page)
            # Absolute page numbers, not a count from this page (mc-iacbw):
            # a card that spills onto a second card page pushes its reading
            # later, and a relative "next N pages" then points at the wrong
            # pages. Printed on every page of the card, from the same
            # card["read"]["pages"] range the manifest carries.
            if card and card.get("read"):
                start, end = card["read"]["pages"]
                # Never "Read pages X to Y, then answer." A card that needs
                # its appendix read before it can be answered is a card that
                # failed to carry its own context; the reading is background,
                # not homework (the cross-reviewer's review, 2026-09-13, mc-9jd4t).
                read_note = _appendix_reference_note(start, end)
                pdf.setFillColor(HexColor("#111111"))
                pdf.setFont("Helvetica-Bold", 8.5)
                pdf.drawString(PAGE_MARGIN_X, note_y, read_note)
            _draw_fragment(pdf, fragment, outline)
        pdf.setFillColor(HexColor("#777777"))
        pdf.setFont("Helvetica", 5.0)
        pdf.drawString(PAGE_MARGIN_X, 14, manifest["page_id"])
        pdf.drawRightString(PAGE_W - PAGE_MARGIN_X, 14, manifest["layout_version"])
        pdf.showPage()


def render_pdf(path: Path, manifest: dict[str, Any]) -> None:
    validate_manifest(manifest)
    try:
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise TodayPageError(
            "PDF generation needs reportlab. Use the system Python or install reportlab."
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    # invariant=1 stops reportlab stamping the wall clock into the PDF, so the
    # same manifest always renders the same bytes. The review receipt binds a
    # reviewer's three yes answers to the PDF's sha256 (contract section 6),
    # and a timestamp that moves on every render would invalidate a receipt
    # that nothing had actually invalidated - the gate would refuse every
    # push, including the reviewed one (found live 2026-09-01 testing the
    # gate). Reproducible bytes are also what let two agents build fixtures
    # against one sample document.
    pdf = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H), invariant=1)
    pdf.setTitle(f"Decisions - {manifest['date']}")
    pdf.setSubject(
        canonical_json(
            {
                "schema_version": manifest["schema_version"],
                "layout_version": manifest["layout_version"],
                "page_id": manifest["page_id"],
                "manifest_hash": manifest["manifest_hash"],
            }
        )
    )
    if manifest["schema_version"] >= 2:
        _render_v3_pages(pdf, manifest)
    else:
        _render_v2_pages(pdf, manifest)
    pdf.save()


def _render_v2_pages(pdf: Any, manifest: dict[str, Any]) -> None:
    """Draw an archived schema-1 document (the v2.x layout, one card stack)."""
    from reportlab.lib.colors import HexColor

    action_entries = [
        entry for entry in manifest["entries"] if entry["kind"] == "action"
    ]
    entries_by_id = {entry["entry_id"]: entry for entry in manifest["entries"]}
    appendices_by_page: dict[int, tuple[dict[str, Any], int, list[str]]] = {}
    for appendix in manifest["appendices"]:
        for index, lines in enumerate(appendix["pages"]):
            appendices_by_page[appendix["page_numbers"][index]] = (
                appendix,
                index,
                lines,
            )
    for page in range(1, manifest["page_count"] + 1):
        pdf.setFillColor(HexColor("#111111"))
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(PAGE_MARGIN_X, PAGE_H - 32, f"Decisions - {manifest['date']}")
        pdf.setFont("Helvetica", 9)
        pdf.drawRightString(
            PAGE_W - PAGE_MARGIN_X,
            PAGE_H - 30,
            f"Page {page} of {manifest['page_count']}",
        )
        pdf.setFillColor(HexColor("#555555"))
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(
            PAGE_MARGIN_X,
            PAGE_H - 46,
            "Rendered "
            f"{manifest['verification']['verified_at_human']}. "
            "Facts are dated where they were not re-checked.",
        )

        appendix_page = appendices_by_page.get(page)
        if appendix_page:
            appendix, page_index, lines = appendix_page
            pdf.setFillColor(HexColor("#111111"))
            pdf.setFont("Helvetica-Bold", 12)
            continuation = " (continued)" if page_index else ""
            pdf.drawString(
                PAGE_MARGIN_X,
                PAGE_H - 70,
                f"Appendix {appendix['appendix']}{continuation}: "
                f"{appendix['label']}",
            )
            text_y = APPENDIX_CONTENT_TOP
            for line in lines:
                text_y = _draw_appendix_line(pdf, line, text_y)
            pdf.setFillColor(HexColor("#777777"))
            pdf.setFont("Helvetica", 5.0)
            pdf.drawString(PAGE_MARGIN_X, 14, manifest["page_id"])
            pdf.drawRightString(
                PAGE_W - PAGE_MARGIN_X, 14, manifest["layout_version"]
            )
            pdf.showPage()
            continue

        pdf.setFillColor(HexColor("#555555"))
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(
            PAGE_MARGIN_X,
            PAGE_H - 60,
            "Tick Y or N clearly. Add a note on the line when the answer needs detail.",
        )
        items: list[tuple[str, dict[str, Any]]] = [
            ("action", entry)
            for entry in action_entries
            if entry["page"] == page
        ] + [
            ("card", card)
            for card in manifest["cards"]
            if card["page"] == page
        ]
        items.sort(key=lambda item: item[1]["layout"]["top"], reverse=True)
        for kind, item in items:
            layout = item["layout"]
            x = layout["x"]
            width = layout["width"]
            top = layout["top"]
            bottom = layout["bottom"]
            pdf.setStrokeColor(HexColor("#BBBBBB"))
            pdf.setLineWidth(0.7)
            pdf.roundRect(x, bottom, width, top - bottom, 6, fill=0, stroke=1)
            if kind == "action":
                _draw_box(pdf, item["boxes"][0]["bbox"], "RUN")
                text_y = top - CARD_PADDING - BODY_FONT_SIZE
                pdf.setFont("Helvetica-Bold", BODY_FONT_SIZE)
                pdf.setFillColor(HexColor("#111111"))
                for line in layout["label_lines"]:
                    pdf.drawString(x + 38.0, text_y, line)
                    text_y -= BODY_LINE_HEIGHT
                continue

            text_x = x + CARD_PADDING
            if layout.get("system_label") and layout.get("system_y") is not None:
                pdf.setFillColor(HexColor("#666666"))
                pdf.setFont("Helvetica-Bold", SYSTEM_LABEL_FONT_SIZE)
                pdf.drawString(
                    text_x,
                    layout["system_y"],
                    f"System: {layout['system_label']}",
                )
            pdf.setFillColor(HexColor("#111111"))
            pdf.setFont("Helvetica-Bold", QUESTION_FONT_SIZE)
            text_y = layout["question_y"]
            for line in layout["question_lines"]:
                pdf.drawString(text_x, text_y, line)
                text_y -= QUESTION_LINE_HEIGHT

            pdf.setFillColor(HexColor("#666666"))
            pdf.setFont("Helvetica", 8)
            pdf.drawString(text_x, layout["dates_y"], item["dates"])

            pdf.setFillColor(HexColor("#111111"))
            pdf.setFont("Helvetica", BODY_FONT_SIZE)
            text_y = layout["description_y"]
            for line in layout["description_lines"]:
                pdf.drawString(text_x, text_y, line)
                text_y -= BODY_LINE_HEIGHT

            pdf.setFont("Helvetica-Bold", BODY_FONT_SIZE)
            text_y = layout["recommendation_y"]
            for line in layout["recommendation_lines"]:
                pdf.drawString(text_x, text_y, line)
                text_y -= BODY_LINE_HEIGHT

            pdf.setFont("Helvetica-Bold", BODY_FONT_SIZE)
            pdf.drawString(text_x, layout["decision_heading_y"], "Decision")
            for row in layout["decision_rows"]:
                entry = entries_by_id[row["entry_id"]]
                pdf.setFont("Helvetica", BODY_FONT_SIZE)
                text_y = row["text_y"]
                for line in row["lines"]:
                    pdf.drawString(text_x, text_y, line)
                    text_y -= BODY_LINE_HEIGHT
                _draw_box(pdf, entry["boxes"][0]["bbox"], "Y")
                _draw_box(pdf, entry["boxes"][1]["bbox"], "N")

            line_x0, line_y0, _, _ = layout["write_in_line"]
            pdf.setFillColor(HexColor("#555555"))
            pdf.setFont("Helvetica", 8)
            pdf.drawString(line_x0 - 34.0, line_y0 - 1.0, "Note:")
            pdf.setStrokeColor(HexColor("#777777"))
            pdf.setLineWidth(0.5)
            for write_in_line in layout["write_in_lines"]:
                pdf.line(*write_in_line)
        pdf.setFillColor(HexColor("#777777"))
        pdf.setFont("Helvetica", 5.0)
        pdf.drawString(PAGE_MARGIN_X, 14, manifest["page_id"])
        pdf.drawRightString(PAGE_W - PAGE_MARGIN_X, 14, manifest["layout_version"])
        pdf.showPage()


def generate(
    review_queue: Path,
    actions_path: Path | None,
    verify_report: Path,
    out_dir: Path,
    date: str,
    lock_timeout: float = QUEUE_LOCK_TIMEOUT_SECONDS,
    answer_record_root: Path = ANSWER_RECORD_ROOT,
    extra_held: list[dict[str, Any]] | None = None,
    state_path: Path = QUESTION_STATE_PATH,
    only_rows: list[str] | None = None,
    document_name: str | None = None,
    cover_note: str | None = None,
) -> tuple[Path | None, Path]:
    dt.date.fromisoformat(date)
    actions = load_actions(actions_path)
    # ONE snapshot of the queue, and both the printed rows and the attested
    # hash come out of it. Parsing the file and hashing the file used to be
    # two separate reads with nothing between them but luck: a writer landing
    # in the gap produced a page that printed the old rows under a hash of the
    # new file - a question re-asked while the manifest swore its input was
    # current, which is the exact failure this chain exists to prevent
    # (mc-63x2). The lock keeps the read away from a half-written file; it is
    # held for the read alone and it has a timeout, so the 06:00 job fails
    # loudly rather than wedging behind another writer.
    # question_state imports this module, so it is imported here rather than
    # at the top: a module-level import would be a cycle.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import question_state

    # Lock order is queue first, then state, released in reverse - the same
    # order the consumer takes them, so the two can never deadlock against
    # each other. Both are read inside the pair, because a row read at 06:00
    # against state read a moment earlier could print a question the reader had
    # just answered. A malformed or newer state file raises out of here and
    # stops the run: printing while blind to what he has already settled is
    # the failure this whole chain exists to prevent.
    try:
        with queue_lock(review_queue, timeout=lock_timeout):
            queue_bytes = Path(review_queue).read_bytes()
            with question_state.locked_state(state_path) as state:
                question_state_snapshot = json.loads(json.dumps(state))
    except question_state.QuestionStateError as exc:
        raise TodayPageError(
            f"The record of what you have already answered could not be read "
            f"({exc}). No document was generated: printing while blind to "
            "that would re-ask a question you have settled."
        ) from exc
    # The GitHub freshness gate is bound to the date this document renders
    # for, not to the wall clock, so a render for an earlier date cannot be
    # waved through by today's checks (mc-9jd4t).
    decisions = parse_review_queue_bytes(
        queue_bytes, render_date=date, queue_path=review_queue
    )
    report = load_verification_report(verify_report)
    verified_titles = set(report["verified_titles"])
    missing = [
        decision["title"]
        for decision in decisions
        if clean_markdown(decision["title"]) not in verified_titles
    ]
    if missing:
        raise TodayPageError(
            "The filtered queue contains an item that was not verified current: "
            + ", ".join(missing)
        )
    # Read the durable answer record BEFORE anything is rendered. This raises
    # on an unreadable record, which is the intended behaviour: no document at
    # all beats a document that re-asks a question the reader already answered.
    recorded = load_recorded_answers(
        queue_bytes.decode("utf-8"), answer_record_root
    )
    decisions, excluded = filter_already_answered(decisions, recorded)
    settled_history = load_settled_question_history(
        question_state_snapshot, answer_record_root
    )
    decisions, settled, state_excluded = filter_settled_questions(
        decisions, question_state_snapshot, settled_history, render_date=date
    )
    excluded.extend(state_excluded)
    # A named subset of rows (mc-9jd4t): a supplement carries exactly the
    # cards it names and nothing else. Every name has to match a row that
    # survived the filters above, or the caller asked for a card this
    # document cannot print and gets told which, rather than a short
    # document that looks complete.
    if only_rows is not None:
        available = {
            decision.get("row_key")
            for decision in decisions
            if decision.get("row_key")
        }
        missing_rows = [key for key in only_rows if key not in available]
        if missing_rows:
            raise TodayPageError(
                "These rows were asked for but are not available to print: "
                + ", ".join(missing_rows)
                + ". No document was generated."
            )
        wanted = list(only_rows)
        decisions = sorted(
            (
                decision
                for decision in decisions
                if decision.get("row_key") in set(wanted)
            ),
            key=lambda decision: wanted.index(decision["row_key"]),
        )
    verification = {
        "verified_at": report["verified_at"],
        "verified_at_human": report["verified_at_human"],
    }
    hashes = {
        "review_queue": sha256_bytes(queue_bytes),
        "verify_report": sha256_file(verify_report),
    }
    if actions_path:
        hashes["actions"] = sha256_file(actions_path)
    manifest = build_manifest(
        date,
        actions,
        decisions,
        hashes,
        verification=verification,
        excluded_already_answered=excluded,
        held=list(report.get("held") or []) + list(extra_held or []),
        settled=settled,
        document_name=document_name,
        cover_note=cover_note,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    name = document_name or f"decisions-{date}"
    pdf_path = out_dir / f"{name}.pdf"
    manifest_path = out_dir / f"{name}.manifest.json"
    accounted_titles = {
        clean_markdown(str(row.get("title", "")))
        for row in excluded + settled
    }
    # Store the post-answer accounting with the manifest so generated and
    # already-rendered delivery use the same tripwire, including unknown rows.
    manifest["unresolved_active_titles"] = [
        title for title in report["verified_titles"]
        if title not in accounted_titles
    ]
    manifest["manifest_hash"] = manifest_digest(manifest)
    if not manifest["cards"]:
        atomic_json(manifest_path, manifest)
        return None, manifest_path
    render_pdf(pdf_path, manifest)
    manifest["artifact_hashes"] = {"pdf": sha256_file(pdf_path)}
    atomic_json(manifest_path, manifest)
    return pdf_path, manifest_path


def _expanded(bounds: list[float], amount: float = BOX_TOLERANCE) -> list[float]:
    return [bounds[0] - amount, bounds[1] - amount, bounds[2] + amount, bounds[3] + amount]


def _intersects(left: list[float], right: list[float]) -> bool:
    return not (
        left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3]
    )


def _inside(x: float, y: float, bounds: list[float]) -> bool:
    return bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]


def _overlap_area(bounds: list[float], region: list[float]) -> float | None:
    """How much of a stroke's bounds lies inside a region, or None if none.

    A stroke reduced to a single point (or a perfectly straight hairline) has
    zero extent on one axis, so a touching overlap scores 0.0 rather than
    being reported as no overlap at all.
    """
    dx = min(bounds[2], region[2]) - max(bounds[0], region[0])
    dy = min(bounds[3], region[3]) - max(bounds[1], region[1])
    if dx < 0.0 or dy < 0.0:
        return None
    return dx * dy


def _stroke_bbox(stroke: dict[str, Any]) -> list[float]:
    points = stroke.get("points")
    if not isinstance(points, list) or not points:
        raise TodayPageError("Each stroke needs at least one [x, y] point.")
    try:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
    except (IndexError, TypeError, ValueError) as exc:
        raise TodayPageError("Stroke points must be numeric [x, y] pairs.") from exc
    return [min(xs), min(ys), max(xs), max(ys)]


def device_to_pdf(
    x: float,
    y: float,
    paper_width: float,
    paper_height: float,
) -> list[float]:
    """Map v6 logical line coordinates into Letter PDF points.

    SceneInfo.paper_size identifies the device profile. It does not define the
    coordinate domain used by Line points. The observed 1404x1872 profile uses
    centered X and top-down Y in a 1920-unit logical width.
    """
    profile = COORDINATE_PROFILES.get((paper_width, paper_height))
    if profile is None:
        raise TodayPageError(
            "The v6 page uses an unknown coordinate profile. No ink was read."
        )
    scale = PAGE_W / profile["logical_width"]
    pdf_x = PAGE_W / 2 + x * scale
    pdf_y = PAGE_H - y * scale
    return [round(pdf_x, 3), round(pdf_y, 3)]


def _cpages_order(c_pages: Any) -> tuple[str, list[str]]:
    """Resolve one cPages block's page order.

    mc-1jw9 root cause: the tablet's newer content-file format (first seen live
    2026-08-02, a fresh re-push) omits "redir" on a page that was appended
    after the document's original creation - that page has no "original"
    position to redirect from. The old parser treated any missing redir as
    fatal, so the WHOLE document read zero ink. "idx" (a lexically sortable
    fractional position key) is present and consistent on every real sample
    seen so far, including the page missing redir, so it is now the PRIMARY
    ordering signal; redir is an optional cross-check, enforced only when
    every page provides it, and array position is the last-resort fallback
    (the tablet's own literal page list) rather than a hard failure.
    """
    if not isinstance(c_pages, dict) or "pages" not in c_pages:
        raise NoInkReadError(
            "The archive page order is incomplete or ambiguous. No ink was read."
        )
    pages = c_pages["pages"]
    if not isinstance(pages, list) or not pages:
        raise NoInkReadError(
            "The archive page order is incomplete or ambiguous. No ink was read."
        )
    page_ids: list[str] = []
    idx_values: list[str | None] = []
    redir_values: list[int | None] = []
    for position, page in enumerate(pages):
        if not isinstance(page, dict):
            raise NoInkReadError(
                f"Archive page at position {position} is not a recognizable "
                "page record. No ink was read."
            )
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise NoInkReadError(
                f"Archive page at position {position} has no usable id. "
                "No ink was read."
            )
        page_ids.append(page_id)
        idx_field = page.get("idx")
        idx_value = idx_field.get("value") if isinstance(idx_field, dict) else None
        idx_values.append(idx_value if isinstance(idx_value, str) and idx_value else None)
        redir_field = page.get("redir")
        redir_value = redir_field.get("value") if isinstance(redir_field, dict) else None
        redir_values.append(
            redir_value
            if isinstance(redir_value, int) and not isinstance(redir_value, bool)
            else None
        )
    if len(set(page_ids)) != len(page_ids):
        raise NoInkReadError(
            "The archive page order contains duplicate page ids. No ink was read."
        )

    idx_order: list[str] | None = None
    if all(value is not None for value in idx_values) and len(set(idx_values)) == len(idx_values):
        idx_order = [page_id for _, page_id in sorted(zip(idx_values, page_ids))]

    redir_order: list[str] | None = None
    provided_redirs = [value for value in redir_values if value is not None]
    if len(provided_redirs) == len(pages):
        if sorted(redir_values) != list(range(len(pages))):
            raise NoInkReadError(
                "The archive page redir values are not a complete, unique "
                "ordering. No ink was read."
            )
        redir_order = [""] * len(pages)
        for redir_value, page_id in zip(redir_values, page_ids):
            redir_order[redir_value] = page_id

    if idx_order is not None and redir_order is not None and idx_order != redir_order:
        raise NoInkReadError(
            "The archive page order sources (idx vs redir) disagree. "
            "No ink was read."
        )
    if idx_order is not None:
        return "cPages_idx", idx_order
    if redir_order is not None:
        return "cPages_redir", redir_order
    return "cPages_array_order", page_ids


def _ordered_archive_page_ids(content: dict[str, Any]) -> tuple[list[str], str]:
    if not isinstance(content, dict):
        raise NoInkReadError(
            "The archive page order is incomplete or ambiguous. No ink was read."
        )
    ordered_sources: list[tuple[str, list[str]]] = []

    if "cPages" in content:
        ordered_sources.append(_cpages_order(content["cPages"]))

    if "pages" in content:
        pages = content["pages"]
        if (
            not isinstance(pages, list)
            or any(not isinstance(page_id, str) or not page_id for page_id in pages)
        ):
            raise NoInkReadError(
                "The archive page order is incomplete or ambiguous. No ink was read."
            )
        ordered_sources.append(("legacy_pages", list(pages)))

    if not ordered_sources:
        raise NoInkReadError(
            "The archive page order is incomplete or ambiguous. No ink was read."
        )
    if any(len(page_ids) != len(set(page_ids)) for _, page_ids in ordered_sources):
        raise NoInkReadError(
            "The archive page order contains duplicate page ids. No ink was read."
        )
    first_ids = ordered_sources[0][1]
    if any(page_ids != first_ids for _, page_ids in ordered_sources[1:]):
        raise NoInkReadError(
            "The archive page formats disagree. No ink was read."
        )
    source_name = (
        "both_verified" if len(ordered_sources) == 2 else ordered_sources[0][0]
    )
    return first_ids, source_name


def _card_lookup(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {card["card_id"]: card for card in manifest["cards"]}


def _entry_label(entry: dict[str, Any], cards_by_id: dict[str, Any]) -> str:
    """The human-readable label for an entry: the action's own label, or the
    parent card's headline for a decision entry (mc-1jw9: used so ambiguity
    and note reports name a card the reader recognizes, not an opaque entry id)."""
    if entry["kind"] == "action":
        return entry["label"]
    card = cards_by_id.get(entry.get("card_id"))
    return card["headline"] if card else entry["label"]


def resolve(manifest: dict[str, Any], strokes_doc: dict[str, Any]) -> dict[str, Any]:
    """Resolve ink into decisions/actions, ambiguities, and note regions.

    mc-1jw9 (defect 1): fail-closed is PER MARK, not per document. A stroke
    that cannot be placed cleanly quarantines only the entry it touches;
    every other entry with a clean, single mark resolves normally so a caller
    can apply the clean answers and ask about only the ambiguous ones.
    """
    if manifest.get("schema_version") == 2:
        from decision_resolver_v3 import resolve_v3

        return resolve_v3(manifest, strokes_doc)
    validate_manifest(manifest)
    for key in ("schema_version", "layout_version", "page_id"):
        if strokes_doc.get(key) != manifest.get(key):
            raise TodayPageError(
                f"Ink does not match this page ({key} differs). No action was taken."
            )
    cards_by_id = _card_lookup(manifest)
    boxes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in manifest["entries"]:
        for entry_box in entry["boxes"]:
            boxes.append((entry, entry_box))

    # mc-1jw9 (defect 4): the reader's overflow ink often spills past the two ruled
    # note lines into the blank space below a card (the missed complaint note
    # on 2026-08-02 - "You've been missing a lot of answers..." - did exactly
    # this). The capture region extends down to whatever comes next on the
    # page (the next card's top, or the page's bottom content margin) so
    # overflow handwriting is never silently dropped as an unclassified
    # "ignored" stroke.
    cards_by_page: dict[int, list[dict[str, Any]]] = {}
    for card in manifest["cards"]:
        cards_by_page.setdefault(card["page"], []).append(card)
    write_in_regions: list[tuple[str, int, list[float]]] = []
    for page, page_cards in cards_by_page.items():
        ordered = sorted(page_cards, key=lambda c: c["layout"]["top"], reverse=True)
        for position, card in enumerate(ordered):
            write_bbox = card["layout"]["write_in_bbox"]
            lower_bound = (
                ordered[position + 1]["layout"]["top"]
                if position + 1 < len(ordered)
                else PAGE_CONTENT_BOTTOM
            )
            # mc-4a56: the band's vertical extent alone says which card the
            # ink belongs to - every card spans the same content width, so
            # clipping the region to the card's own x range only threw away
            # note ink that ran into the page margin (2026-08-25 card-001 lost
            # the end of "documents," that way). Span the full page width.
            write_in_regions.append(
                (
                    card["card_id"],
                    page,
                    [0.0, lower_bound, PAGE_W, write_bbox[3]],
                )
            )

    hits: dict[str, set[str]] = {}
    evidence: dict[str, list[str]] = {}
    ambiguities: list[dict[str, Any]] = []
    ignored: list[str] = []
    note_hits: dict[str, list[str]] = {}
    for index, stroke in enumerate(strokes_doc.get("strokes", [])):
        stroke_id = str(stroke.get("stroke_id", f"stroke-{index + 1}"))
        stroke_page = stroke.get("page")
        if (
            isinstance(stroke_page, bool)
            or not isinstance(stroke_page, int)
            or not 1 <= stroke_page <= manifest["page_count"]
        ):
            raise TodayPageError(
                "Each stroke needs a valid 1-based page number. No action was taken."
            )
        bounds = _stroke_bbox(stroke)
        cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
        page_boxes = [
            entry_box["bbox"]
            for entry, entry_box in boxes
            if entry["page"] == stroke_page
        ]
        # mc-4a56: a stroke whose centre lands in a band still belongs to that
        # band (unchanged). What changed is the fallback: a stroke whose centre
        # lands in NO band, but which overlaps one, now belongs to the band it
        # overlaps most instead of being dropped. The reader's first note line is
        # written on or just above the top ruled line, so the centre of several
        # letters sits a point or two above the band - the centre-only test
        # silently discarded exactly those strokes and produced gap-toothed
        # crops (2026-08-25: card-001 lost ten strokes out of its first line,
        # card-003 lost the crossbar that starts "Try harder").
        write_hit = None
        best_rank: tuple[int, float, int] | None = None
        for order, (card_id, region_page, region) in enumerate(write_in_regions):
            if region_page != stroke_page:
                continue
            area = _overlap_area(bounds, region)
            if area is None:
                continue
            rank = (1 if _inside(cx, cy, region) else 0, area, -order)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                write_hit = card_id
        # mc-4a56: ink in a note band is an ANSWER only if its centre lands in
        # an answer box. The old test asked whether the stroke's bounds merely
        # touched a box's 4pt tolerance margin, which is true of any long word
        # written on the top ruled line - page-1-line-22 on 2026-08-25 is the
        # word "cument", 81pt wide against a measured tick range of 15.5-21.7pt,
        # and it was quarantined as a mark against decision-001b (already
        # answered NO by the real X, page-1-line-52/53). That cost the crop the
        # middle of "documents," AND asked the reader to re-answer a decision he had
        # answered. No answer box overlaps any note band by construction (a
        # card's boxes sit above its own write-in area, and the band stops at
        # the next card's top), so a centre-in-box test cannot swallow a tick.
        if write_hit is not None and not any(
            _inside(cx, cy, _expanded(bounds_box)) for bounds_box in page_boxes
        ):
            ignored.append(stroke_id)
            note_hits.setdefault(write_hit, []).append(stroke_id)
            continue
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if width > GIANT_MARK_SIZE or height > GIANT_MARK_SIZE:
            ambiguities.append(
                {
                    "stroke_id": stroke_id,
                    "reason": "mark_too_large",
                    "bbox": bounds,
                    "page": stroke_page,
                }
            )
            continue
        intersecting = [
            (entry, entry_box)
            for entry, entry_box in boxes
            if entry["page"] == stroke_page
            if _intersects(bounds, _expanded(entry_box["bbox"]))
        ]
        raw_intersecting = [
            (entry, entry_box)
            for entry, entry_box in boxes
            if entry["page"] == stroke_page
            if _intersects(bounds, entry_box["bbox"])
        ]
        centered = [
            (entry, entry_box)
            for entry, entry_box in intersecting
            if _inside(cx, cy, _expanded(entry_box["bbox"]))
        ]
        center_raw_disagree = (
            len(centered) == 1
            and len(raw_intersecting) == 1
            and (
                centered[0][0]["entry_id"],
                centered[0][1]["choice"],
            )
            != (
                raw_intersecting[0][0]["entry_id"],
                raw_intersecting[0][1]["choice"],
            )
        )
        # A real tick may extend toward the vertically adjacent subquestion.
        # Its bounds can enter that box's tolerance margin without touching the
        # box itself. One centered box wins in that case. Preserve quarantine
        # when the mark actually touches multiple boxes, centers on multiple
        # boxes, or has no center match and only the margins are ambiguous.
        crosses_boxes = (
            len(centered) > 1
            or len(raw_intersecting) > 1
            or center_raw_disagree
            or (not centered and len(intersecting) > 1)
        )
        if crosses_boxes:
            if center_raw_disagree:
                candidates_source = centered + raw_intersecting
            elif len(centered) > 1:
                candidates_source = centered
            elif len(raw_intersecting) > 1:
                candidates_source = raw_intersecting
            else:
                candidates_source = intersecting
            candidates = sorted(
                {entry["entry_id"] for entry, _ in candidates_source}
            )
            ambiguities.append(
                {
                    "stroke_id": stroke_id,
                    "reason": "mark_crosses_boxes",
                    "bbox": bounds,
                    "page": stroke_page,
                    "candidate_entry_ids": candidates,
                    "candidate_labels": sorted(
                        {
                            _entry_label(entry, cards_by_id)
                            for entry, _ in candidates_source
                        }
                    ),
                }
            )
            continue
        if not centered:
            # mc-4a56: ink that grazes a single answer box but is not centred
            # on it is not an answer. If it lies in a card's note band it is
            # note handwriting whose ascenders reached up into the box's
            # tolerance margin, so keep it in the crop instead of discarding
            # it (2026-08-25: the "d" of "documents" under card-001).
            ignored.append(stroke_id)
            if write_hit is not None:
                note_hits.setdefault(write_hit, []).append(stroke_id)
            continue
        entry, entry_box = centered[0]
        hits.setdefault(entry["entry_id"], set()).add(entry_box["choice"])
        evidence.setdefault(entry["entry_id"], []).append(stroke_id)

    actions: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for entry in manifest["entries"]:
        choices = sorted(hits.get(entry["entry_id"], set()))
        if not choices:
            continue
        if len(choices) > 1:
            ambiguities.append(
                {
                    "entry_id": entry["entry_id"],
                    "reason": "multiple_choices",
                    "choices": choices,
                    "page": entry["page"],
                    "label": _entry_label(entry, cards_by_id),
                }
            )
            continue
        choice = choices[0]
        common = {
            "entry_id": entry["entry_id"],
            "label": entry["label"],
            "page": entry["page"],
            "choice": choice,
            "stroke_ids": evidence[entry["entry_id"]],
        }
        if entry["kind"] == "action":
            common["action"] = entry["action"]
            actions.append(common)
        else:
            common["item_id"] = entry["item_id"]
            common["card_id"] = entry["card_id"]
            common["decision_number"] = entry["decision_number"]
            decisions.append(common)

    # mc-1jw9 (defect 4): every note-line region with ink is named here so the
    # caller can render and surface it - nothing handwritten is left as an
    # opaque ignored_strokes id.
    note_regions = [
        {
            "card_id": card_id,
            "page": cards_by_id[card_id]["page"],
            "label": cards_by_id[card_id]["headline"],
            "stroke_ids": stroke_ids,
        }
        for card_id, stroke_ids in note_hits.items()
        if card_id in cards_by_id
    ]

    ink_hash = sha256_bytes(canonical_json(strokes_doc.get("strokes", [])).encode())
    return {
        "schema_version": manifest["schema_version"],
        "layout_version": manifest["layout_version"],
        "page_id": manifest["page_id"],
        "ink_hash": ink_hash,
        "status": "needs_review" if ambiguities else "resolved",
        "actions": actions,
        "decisions": decisions,
        "ambiguities": ambiguities,
        "ignored_strokes": ignored,
        "note_regions": note_regions,
    }


def extract_v6(manifest: dict[str, Any], archive: Path) -> dict[str, Any]:
    validate_manifest(manifest)
    try:
        from rmscene import read_tree
        from rmscene.scene_items import Line
    except ImportError as exc:
        raise TodayPageError(
            "V6 extraction needs rmscene 0.8.0. Install "
            "requirements.txt (pip install -r requirements.txt)."
        ) from exc

    with zipfile.ZipFile(archive) as bundle:
        pdf_names = [name for name in bundle.namelist() if name.endswith(".pdf")]
        if len(pdf_names) != 1:
            raise TodayPageError("The archive must contain exactly one source PDF.")
        source_pdf = bundle.read(pdf_names[0])
        if (
            manifest["page_id"].encode("ascii") not in source_pdf
            or manifest["manifest_hash"].encode("ascii") not in source_pdf
        ):
            raise TodayPageError(
                "The archive PDF does not match this manifest. No ink was read."
            )
        content_names = [name for name in bundle.namelist() if name.endswith(".content")]
        if len(content_names) != 1:
            raise TodayPageError("The archive must contain exactly one .content file.")
        content = json.loads(bundle.read(content_names[0]))
        ordered_page_ids, page_order_format = _ordered_archive_page_ids(content)
        expected_count = manifest["page_count"]
        if len(ordered_page_ids) < expected_count:
            raise NoInkReadError(
                f"The archive has {len(ordered_page_ids)} page(s) but this "
                f"manifest expects {expected_count}. No ink was read."
            )
        # mc-1jw9: writing past the last manifest page makes the tablet append
        # a new page (live 2026-08-02). That page has no question layout to
        # place ink against; report it instead of failing the whole document.
        extra_page_ids = ordered_page_ids[expected_count:]
        in_scope_page_ids = ordered_page_ids[:expected_count]

        rm_names_by_id: dict[str, list[str]] = {}
        for name in bundle.namelist():
            if not name.endswith(".rm"):
                continue
            rm_page_id = Path(name).name.removesuffix(".rm")
            rm_names_by_id.setdefault(rm_page_id, []).append(name)
        unexpected_rm_ids = sorted(set(rm_names_by_id) - set(ordered_page_ids))
        if unexpected_rm_ids:
            raise TodayPageError(
                "The archive contains ink for an unknown page. No ink was read."
            )
        duplicate_rm_ids = sorted(
            page_id for page_id, names in rm_names_by_id.items() if len(names) != 1
        )
        if duplicate_rm_ids:
            raise TodayPageError(
                "The archive contains duplicate ink files. No ink was read."
            )

        trees: list[tuple[int, str, Any | None]] = []
        for page_number, page_id in enumerate(in_scope_page_ids, 1):
            rm_names = rm_names_by_id.get(page_id, [])
            if not rm_names:
                trees.append((page_number, page_id, None))
                continue
            if len(rm_names) != 1:
                raise TodayPageError(
                    f"The archive does not contain one matching v6 page for page "
                    f"{page_number}."
                )
            with bundle.open(rm_names[0]) as raw:
                trees.append((page_number, page_id, read_tree(raw)))

        unplaced_pages: list[dict[str, Any]] = []
        for page_id in extra_page_ids:
            rm_names = rm_names_by_id.get(page_id, [])
            if not rm_names:
                unplaced_pages.append(
                    {
                        "rm_page_id": page_id,
                        "ink_file_status": "absent_assumed_blank",
                        "line_count": 0,
                    }
                )
                continue
            with bundle.open(rm_names[0]) as raw:
                extra_tree = read_tree(raw)
            line_count = sum(
                1
                for item in extra_tree.walk()
                if isinstance(
                    item.value if hasattr(item, "value") else item, Line
                )
                and (item.value if hasattr(item, "value") else item).points
            )
            unplaced_pages.append(
                {
                    "rm_page_id": page_id,
                    "ink_file_status": "present",
                    "line_count": line_count,
                }
            )

    strokes: list[dict[str, Any]] = []
    source_pages: list[dict[str, Any]] = []
    for page_number, page_id, tree in trees:
        if tree is None:
            source_pages.append(
                {
                    "page": page_number,
                    "rm_page_id": page_id,
                    "ink_file_status": "absent_assumed_blank",
                    "paper_size": None,
                    "coordinate_profile": None,
                    "line_counts": {
                        "seen": 0,
                        "kept": 0,
                        "out_of_bounds": 0,
                    },
                }
            )
            continue
        scene_info = getattr(tree, "scene_info", None)
        paper = getattr(scene_info, "paper_size", None)
        if (
            not isinstance(paper, (list, tuple))
            or len(paper) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                for value in paper
            )
        ):
            raise TodayPageError(
                f"The v6 page {page_number} does not provide a usable paper size."
            )
        profile = COORDINATE_PROFILES.get((float(paper[0]), float(paper[1])))
        if profile is None:
            raise TodayPageError(
                f"The v6 page {page_number} uses an unknown coordinate profile. "
                "No ink was read."
            )
        line_counts = {"seen": 0, "kept": 0, "out_of_bounds": 0}
        for item in tree.walk():
            value = item.value if hasattr(item, "value") else item
            if not isinstance(value, Line) or not value.points:
                continue
            line_counts["seen"] += 1
            points = [
                device_to_pdf(
                    float(point.x),
                    float(point.y),
                    float(paper[0]),
                    float(paper[1]),
                )
                for point in value.points
            ]
            bounds = [
                min(point[0] for point in points),
                min(point[1] for point in points),
                max(point[0] for point in points),
                max(point[1] for point in points),
            ]
            page_bounds = [0.0, 0.0, PAGE_W, PAGE_H]
            if _intersects(bounds, page_bounds):
                line_counts["kept"] += 1
                strokes.append(
                    {
                        "stroke_id": f"page-{page_number}-line-{len(strokes) + 1}",
                        "page": page_number,
                        "points": points,
                    }
                )
            else:
                line_counts["out_of_bounds"] += 1
        source_pages.append(
            {
                "page": page_number,
                "rm_page_id": page_id,
                "ink_file_status": "present",
                "paper_size": list(paper),
                "coordinate_profile": profile["name"],
                "line_counts": line_counts,
            }
        )
    return {
        "schema_version": manifest["schema_version"],
        "layout_version": manifest["layout_version"],
        "page_id": manifest["page_id"],
        "source": {
            "archive": archive.name,
            "archive_sha256": sha256_file(archive),
            "pages": source_pages,
            "unplaced_pages": unplaced_pages,
            "page_order_format": page_order_format,
            "coordinate_transform": COORDINATE_TRANSFORM,
            "extractor": "rmscene==0.8.0",
        },
        "strokes": strokes,
    }


# The operator-alert forwarder. It runs hooks.alert from the config and is a
# logged no-op when no hook is configured.
BUS_ALERT_SCRIPT = Path(__file__).resolve().parent / "hooks" / "alert.sh"
NOTE_PNG_SCALE = 4.0  # px per pdf point - keeps small handwriting legible.
NOTE_PNG_PADDING = 6.0


def send_bus_alert(
    subject: str, body: str, slug: str, dry_run: bool = False
) -> dict[str, Any]:
    """Send a monitoring-identity bus v2 alert.

    mc-1jw9 (defect 2): mirrors tools/monitoring/bus-alert.sh EXACTLY by
    invoking it directly (same env file, same recipient, same payload shape)
    rather than reimplementing the mechanics here. dry_run short-circuits
    before any process is started so tests and rehearsals never address a
    real message to the orchestrator.
    """
    if dry_run:
        return {"sent": False, "dry_run": True, "subject": subject, "slug": slug}
    if not BUS_ALERT_SCRIPT.exists():
        return {"sent": False, "error": f"missing {BUS_ALERT_SCRIPT}"}
    try:
        completed = subprocess.run(
            [str(BUS_ALERT_SCRIPT), subject, body, slug],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except OSError as exc:
        return {"sent": False, "error": str(exc)}
    return {
        "sent": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _describe_ambiguity(ambiguity: dict[str, Any]) -> str:
    """One plain-English line naming page, card, and reason for an alert."""
    page = ambiguity.get("page", "?")
    reason = ambiguity.get("reason", "unknown")
    if reason == "multiple_choices":
        label = ambiguity.get("label", "an unlabeled card")
        choices = "/".join(ambiguity.get("choices", []))
        return f'Page {page}: "{label}" - both {choices} were ticked.'
    if reason == "mark_crosses_boxes":
        labels = ambiguity.get("candidate_labels") or ["an unclear card"]
        joined = " / ".join(f'"{label}"' for label in labels)
        return f"Page {page}: a mark crosses more than one answer box near {joined}."
    if reason == "mark_too_large":
        return f"Page {page}: an oversized mark could not be matched to any answer box."
    return f"Page {page}: {reason}."


def render_note_region_pngs(
    manifest: dict[str, Any],
    strokes_doc: dict[str, Any],
    resolution: dict[str, Any],
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Render every note-line region with ink to a cropped PNG for vision-read.

    mc-1jw9 (defect 4): handwritten note-line text was previously visible
    only as opaque ignored_strokes ids. Every note region with ink now gets
    its own PNG next to the resolution artifact so nothing handwritten is
    silently discarded.
    """
    note_regions = resolution.get("note_regions", [])
    if not note_regions:
        return []
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise TodayPageError(
            "Note rendering needs Pillow. Install "
            "requirements.txt (pip install -r requirements.txt)."
        ) from exc

    cards_by_id = _card_lookup(manifest)
    strokes_by_id = {
        str(stroke.get("stroke_id")): stroke
        for stroke in strokes_doc.get("strokes", [])
    }
    notes_dir = out_dir / "notes"
    rendered: list[dict[str, Any]] = []
    for index, region in enumerate(note_regions):
        # mc-z5hy3: a schema-2 region is a cluster of note ink anywhere on a
        # page, so it carries its own bounds and its own file name. A schema-1
        # region is a card's ruled note line and still takes its bounds and
        # its name from that card. One renderer, two shapes of region.
        if isinstance(region.get("bbox"), list) and len(region["bbox"]) == 4:
            bbox = list(region["bbox"])
            name = Path(
                str(region.get("path") or f"page-{region['page']}-note-{index + 1}.png")
            ).name
        else:
            card = cards_by_id.get(region.get("card_id"))
            if card is None:
                continue
            bbox = list(card["layout"]["write_in_bbox"])
            name = f"{region['card_id']}.png"
        # mc-1jw9 (defect 4): overflow ink can spill well past the two ruled
        # lines (the reader's missed 2026-08-02 complaint note reached ~175pt below
        # them). Crop to the union of the ruled area and every captured
        # stroke's real bounds so overflow text is never cut off the render.
        for stroke_id in region["stroke_ids"]:
            stroke = strokes_by_id.get(stroke_id)
            if not stroke or not stroke.get("points"):
                continue
            xs = [point[0] for point in stroke["points"]]
            ys = [point[1] for point in stroke["points"]]
            bbox[0] = min(bbox[0], min(xs))
            bbox[1] = min(bbox[1], min(ys))
            bbox[2] = max(bbox[2], max(xs))
            bbox[3] = max(bbox[3], max(ys))
        x0 = bbox[0] - NOTE_PNG_PADDING
        y0 = bbox[1] - NOTE_PNG_PADDING
        x1 = bbox[2] + NOTE_PNG_PADDING
        y1 = bbox[3] + NOTE_PNG_PADDING
        width = max(1, round((x1 - x0) * NOTE_PNG_SCALE))
        height = max(1, round((y1 - y0) * NOTE_PNG_SCALE))
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        for stroke_id in region["stroke_ids"]:
            stroke = strokes_by_id.get(stroke_id)
            if not stroke:
                continue
            pixels = [
                ((x - x0) * NOTE_PNG_SCALE, (y1 - y) * NOTE_PNG_SCALE)
                for x, y in stroke["points"]
            ]
            if len(pixels) == 1:
                px, py = pixels[0]
                draw.ellipse([px - 2, py - 2, px + 2, py + 2], fill="black")
            else:
                draw.line(pixels, fill="black", width=3, joint="curve")
        notes_dir.mkdir(parents=True, exist_ok=True)
        path = notes_dir / name
        image.save(path, format="PNG")
        rendered.append({**region, "path": str(path)})
    return rendered


def execute(
    resolution: dict[str, Any],
    receipts_dir: Path,
    state_path: Path,
    jobs_path: Path | None,
    review_queue_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    # mc-1jw9 (defect 1): resolve() already fails closed PER MARK - an
    # ambiguous stroke never produces an entry in resolution["actions"]. This
    # function processes only the entries that already resolved cleanly, so a
    # doc-wide ambiguity no longer blocks every other clean answer on it.
    state = json.loads(state_path.read_text()) if state_path.exists() else {"items": {}}
    jobs = json.loads(jobs_path.read_text()) if jobs_path else {}
    results: list[dict[str, Any]] = []
    for candidate in resolution.get("actions", []):
        action = validate_action(
            {
                "type": candidate["action"]["type"],
                "payload": candidate["action"]["payload"],
                "label": candidate["label"],
            }
        )
        receipt_id = sha256_bytes(
            canonical_json(
                {
                    "page_id": resolution["page_id"],
                    "entry_id": candidate["entry_id"],
                }
            ).encode()
        )[:24]
        receipt_path = receipts_dir / f"{receipt_id}.json"
        if receipt_path.exists():
            existing = _read_json(receipt_path)
            status = "already_done" if existing.get("status") == "done" else "needs_review"
            results.append({"entry_id": candidate["entry_id"], "status": status})
            continue
        if not apply:
            results.append({"entry_id": candidate["entry_id"], "status": "dry_run"})
            continue

        receipts_dir.mkdir(parents=True, exist_ok=True)
        claim = {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": receipt_id,
            "page_id": resolution["page_id"],
            "entry_id": candidate["entry_id"],
            "ink_hash": resolution["ink_hash"],
            "status": "claimed",
            "claimed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        try:
            descriptor = os.open(
                receipt_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            results.append({"entry_id": candidate["entry_id"], "status": "needs_review"})
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(claim, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        action_type, payload = action["type"], action["payload"]
        try:
            if action_type == "close_ticket":
                argv = [
                    "bd",
                    "close",
                    payload["ticket_id"],
                    "--reason",
                    payload.get("note", "Approved in the Decisions document"),
                ]
                subprocess.run(argv, check=True)
            elif action_type == "run_job":
                job = jobs.get(payload["job_id"])
                if (
                    not isinstance(job, list)
                    or not job
                    or not all(isinstance(part, str) for part in job)
                ):
                    raise TodayPageError(f"Job is not registered: {payload['job_id']!r}.")
                subprocess.run(job, check=True, shell=False)
            else:
                item_id = payload["item_id"]
                item = state.setdefault("items", {}).setdefault(item_id, {})
                if action_type == "approve_queue_row":
                    if review_queue_path is None:
                        raise TodayPageError(
                            "A review queue path is required to approve a queue row."
                        )
                    close_review_queue_row(review_queue_path, item_id)
                    item["approved_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                elif action_type == "ack_item":
                    item["acknowledged_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                elif action_type == "defer_item":
                    item["deferred_until"] = (
                        dt.date.today() + dt.timedelta(days=payload["days"])
                    ).isoformat()
                atomic_json(state_path, state)
        except Exception as exc:
            claim["status"] = "needs_review"
            claim["error"] = str(exc)
            atomic_json(receipt_path, claim)
            raise
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": receipt_id,
            "page_id": resolution["page_id"],
            "entry_id": candidate["entry_id"],
            "ink_hash": resolution["ink_hash"],
            "action": action,
            "status": "done",
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        atomic_json(receipt_path, receipt)
        results.append(
            {
                "entry_id": candidate["entry_id"],
                "status": "done",
                "receipt": str(receipt_path),
            }
        )
    return {"apply": apply, "results": results}


def process_archive(
    manifest: dict[str, Any],
    archive: Path,
    out_dir: Path,
    receipts_dir: Path,
    state_path: Path,
    jobs_path: Path | None,
    review_queue_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Run the deterministic watcher handoff against one downloaded archive."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # mc-1jw9 (defect 5): never fail on a missing receipts tree - create it up
    # front regardless of apply/dry-run, same as out_dir above.
    receipts_dir.mkdir(parents=True, exist_ok=True)
    strokes = extract_v6(manifest, archive)
    strokes_path = out_dir / "strokes.json"
    atomic_json(strokes_path, strokes)
    resolution = resolve(manifest, strokes)
    if resolution.get("schema_version") == 2:
        from decision_resolver_v3 import render_write_in_pngs

        resolution["write_ins"] = render_write_in_pngs(
            manifest, strokes, resolution, out_dir
        )
        by_id = {
            item["entry_id"]: item for item in resolution["write_ins"]
        }
        for decision in resolution.get("decisions", []):
            decision["write_in"] = by_id.get(decision["entry_id"])
    notes: list[dict[str, Any]] | None = None
    if resolution.get("schema_version") == 2:
        # mc-z5hy3: the schema-2 resolution carries the crop path for every
        # note, the same way it already carries one for every write-in, so a
        # reader of resolution.json alone can open the handwriting. That means
        # rendering before it is written - which schema 2 already does for
        # write-ins, so it adds no new way for this to fail early.
        notes = render_note_region_pngs(manifest, strokes, resolution, out_dir)
        resolution["note_regions"] = notes
    resolution_path = out_dir / "resolution.json"
    atomic_json(resolution_path, resolution)
    if notes is None:
        notes = render_note_region_pngs(manifest, strokes, resolution, out_dir)
    notes_path = out_dir / "notes.json"
    atomic_json(notes_path, notes)
    # mc-1jw9 (defect 1): per-mark fail-closed. execute() now only ever sees
    # entries that already resolved cleanly, so quarantined marks never block
    # the rest of the page's clean answers/actions from applying or dry-running.
    execution = execute(
        resolution,
        receipts_dir=receipts_dir,
        state_path=state_path,
        jobs_path=jobs_path,
        review_queue_path=review_queue_path,
        apply=apply,
    )
    execution_path = out_dir / "execution.json"
    atomic_json(execution_path, execution)
    unplaced_pages = [
        page
        for page in strokes.get("source", {}).get("unplaced_pages", [])
        if page.get("ink_file_status") == "present" and page.get("line_count", 0) > 0
    ]
    return {
        "status": resolution["status"],
        "schema_version": resolution.get("schema_version", 1),
        "strokes": str(strokes_path),
        "resolution": str(resolution_path),
        "execution": str(execution_path),
        "notes": str(notes_path),
        "ambiguities": resolution["ambiguities"],
        "note_regions": notes,
        "unplaced_pages_with_ink": unplaced_pages,
        "apply": apply,
    }


def register_page(
    registry_path: Path,
    device_path: str,
    manifest_path: Path,
    out_dir: Path,
    receipts_dir: Path,
    state_path: Path,
    extractor_python: str,
    jobs_path: Path | None,
    review_queue_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    if not device_path.startswith("/") or device_path.endswith("/"):
        raise TodayPageError("Device path must be an absolute document path.")
    manifest = _read_json(manifest_path)
    validate_manifest(manifest)
    registry = (
        json.loads(registry_path.read_text(encoding="utf-8"))
        if registry_path.exists()
        else {"pages": {}}
    )
    if not isinstance(registry, dict) or not isinstance(registry.get("pages"), dict):
        raise TodayPageError("Decisions registry must contain a pages object.")
    registry["python"] = extractor_python
    config: dict[str, Any] = {
        "manifest": str(manifest_path.resolve()),
        "out_dir": str(out_dir.resolve()),
        "receipts_dir": str(receipts_dir.resolve()),
        "state": str(state_path.resolve()),
        "apply": apply,
        "page_id": manifest["page_id"],
    }
    if jobs_path:
        config["jobs"] = str(jobs_path.resolve())
    if review_queue_path:
        config["review_queue"] = str(review_queue_path.resolve())
    registry["pages"][device_path] = config
    atomic_json(registry_path, registry)
    return {"device_path": device_path, "page_id": manifest["page_id"], "apply": apply}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TodayPageError(f"{path} must contain a JSON object.")
    return value


def _send_pipeline_alert(args: argparse.Namespace, subject: str, body: str, slug: str) -> None:
    dry_run = bool(getattr(args, "alert_dry_run", False))
    outcome = send_bus_alert(subject, body, slug, dry_run=dry_run)
    if dry_run:
        print(f"ALERT DRY RUN (not sent): {subject}", file=sys.stderr)
    elif outcome.get("sent"):
        print(f"Alert sent: {subject}", file=sys.stderr)
    else:
        print(
            f"Alert SEND FAILED ({outcome.get('error') or outcome.get('stderr')}): "
            f"{subject}",
            file=sys.stderr,
        )


def _process_archive_alert_body(manifest: dict[str, Any], result: dict[str, Any]) -> str:
    what = "pen marks" if result["ambiguities"] else "handwriting"
    lines = [
        f"reMarkable Decisions {manifest.get('date', '?')}: {what} need your read.",
        "Every other clean answer on this page was still applied/dry-run normally.",
        "",
    ]
    for ambiguity in result["ambiguities"]:
        lines.append("- " + _describe_ambiguity(ambiguity))
    if result.get("unplaced_pages_with_ink"):
        lines.append("")
        lines.append("Extra page(s) beyond this document's layout carry ink:")
        for page in result["unplaced_pages_with_ink"]:
            lines.append(
                f"- {page['rm_page_id']} ({page['line_count']} line(s)) - not "
                "tied to any question; pull the doc by hand to read it."
            )
    if result.get("note_regions"):
        lines.append("")
        lines.append("Handwritten note lines needing a vision-read:")
        for note in result["note_regions"]:
            lines.append(f'- Page {note["page"]}: "{note["label"]}" -> {note["path"]}')
    lines.append("")
    lines.append(f"Full detail: {result['resolution']}")
    return "\n".join(lines)


def _run_process_archive(args: argparse.Namespace) -> int:
    manifest = _read_json(args.manifest)
    result = process_archive(
        manifest,
        args.archive,
        args.out_dir,
        args.receipts_dir,
        args.state,
        args.jobs,
        args.review_queue,
        args.apply,
    )
    print(canonical_json(result))
    # mc-1jw9 (defects 1+2+3): ambiguous marks and unplaced-but-inked pages are
    # the "needs a human read" outcomes - distinct exit code, dedicated alert
    # naming each one. A clean day with only a note on an expected note line
    # is a success (exit 0); its PNG still rides the standard annotation
    # -changed alert via the notes.json/result payload.
    needs_review = bool(result["ambiguities"]) or bool(result["unplaced_pages_with_ink"])
    # mc-z5hy3: on a schema-2 document, handwriting nobody has read is a
    # reason to alert too - nothing else surfaced it, which is how four notes
    # reached nobody on 2026-09-03. It is NOT a reason to fail the run: the
    # answers resolved cleanly, and the consumer only records them when
    # process-archive exits 0. Schema-1 notes keep mc-1jw9's rule (a note on
    # an expected note line is a clean day; its PNG rides the standard
    # annotation-changed alert).
    notes = (
        result.get("note_regions") or []
        if manifest.get("schema_version") == 2
        else []
    )
    if needs_review or notes:
        parts = []
        if needs_review:
            parts.append(f"{len(result['ambiguities'])} mark(s) need your read")
        if notes:
            parts.append(f"{len(notes)} handwritten note(s) need your read")
        subject = f"reMarkable Decisions {manifest.get('date', '?')}: " + ", ".join(parts)
        _send_pipeline_alert(
            args, subject, _process_archive_alert_body(manifest, result), "remarkable-quarantine"
        )
    return EXIT_NEEDS_REVIEW if needs_review else EXIT_OK


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate")
    generate_parser.add_argument(
        "--queue",
        "--review-queue",
        dest="review_queue",
        type=Path,
        required=True,
    )
    generate_parser.add_argument("--verify-report", type=Path, required=True)
    generate_parser.add_argument("--actions", type=Path)
    generate_parser.add_argument("--out-dir", type=Path, required=True)
    generate_parser.add_argument("--date", default=dt.date.today().isoformat())
    # mc-iacbw section 5: cards a reviewer failed are removed and named on the
    # cover page. The reviewer writes them here; nothing disappears quietly.
    generate_parser.add_argument("--held", type=Path)
    generate_parser.add_argument("--state", type=Path, default=QUESTION_STATE_PATH)
    # A supplement (mc-9jd4t): the same rows, the same verification, a
    # named subset of cards, its own name and its own first sentence.
    generate_parser.add_argument(
        "--only-row",
        dest="only_rows",
        action="append",
        help="Print only this row key. Repeatable; order is the card order.",
    )
    generate_parser.add_argument("--document-name")
    generate_parser.add_argument("--cover-note")

    resolve_parser = commands.add_parser("resolve")
    resolve_parser.add_argument("--manifest", type=Path, required=True)
    resolve_parser.add_argument("--strokes", type=Path, required=True)
    resolve_parser.add_argument("--out", type=Path, required=True)

    extract_parser = commands.add_parser("extract-v6")
    extract_parser.add_argument("--manifest", type=Path, required=True)
    extract_parser.add_argument("--archive", type=Path, required=True)
    extract_parser.add_argument("--out", type=Path, required=True)

    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--resolution", type=Path, required=True)
    execute_parser.add_argument("--receipts-dir", type=Path, required=True)
    execute_parser.add_argument("--state", type=Path, required=True)
    execute_parser.add_argument("--jobs", type=Path)
    execute_parser.add_argument("--review-queue", type=Path)
    execute_parser.add_argument("--apply", action="store_true")

    process_parser = commands.add_parser("process-archive")
    process_parser.add_argument("--manifest", type=Path, required=True)
    process_parser.add_argument("--archive", type=Path, required=True)
    process_parser.add_argument("--out-dir", type=Path, required=True)
    process_parser.add_argument("--receipts-dir", type=Path, required=True)
    process_parser.add_argument("--state", type=Path, required=True)
    process_parser.add_argument("--jobs", type=Path)
    process_parser.add_argument("--review-queue", type=Path)
    process_parser.add_argument("--apply", action="store_true")
    process_parser.add_argument(
        "--alert-dry-run",
        action="store_true",
        help="Compute the alert but never send it (tests/rehearsal only).",
    )

    register_parser = commands.add_parser("register")
    register_parser.add_argument("--registry", type=Path, required=True)
    register_parser.add_argument("--device-path", required=True)
    register_parser.add_argument("--manifest", type=Path, required=True)
    register_parser.add_argument("--out-dir", type=Path, required=True)
    register_parser.add_argument("--receipts-dir", type=Path, required=True)
    register_parser.add_argument("--state", type=Path, required=True)
    register_parser.add_argument("--extractor-python", required=True)
    register_parser.add_argument("--jobs", type=Path)
    register_parser.add_argument("--review-queue", type=Path)
    register_parser.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            extra_held = []
            if getattr(args, "held", None):
                extra_held = _read_json(args.held).get("held", [])
            pdf, manifest = generate(
                args.review_queue,
                args.actions,
                args.verify_report,
                args.out_dir,
                args.date,
                extra_held=extra_held,
                state_path=args.state,
                only_rows=args.only_rows,
                document_name=args.document_name,
                cover_note=args.cover_note,
            )
            print(canonical_json({"pdf": str(pdf) if pdf else None, "manifest": str(manifest)}))
            if pdf is None:
                return EXIT_NO_CARDS
        elif args.command == "resolve":
            result = resolve(_read_json(args.manifest), _read_json(args.strokes))
            atomic_json(args.out, result)
            print(args.out)
        elif args.command == "extract-v6":
            result = extract_v6(_read_json(args.manifest), args.archive)
            atomic_json(args.out, result)
            print(args.out)
        elif args.command == "execute":
            result = execute(
                _read_json(args.resolution),
                args.receipts_dir,
                args.state,
                args.jobs,
                args.review_queue,
                args.apply,
            )
            print(canonical_json(result))
        elif args.command == "process-archive":
            return _run_process_archive(args)
        elif args.command == "register":
            result = register_page(
                args.registry,
                args.device_path,
                args.manifest,
                args.out_dir,
                args.receipts_dir,
                args.state,
                args.extractor_python,
                args.jobs,
                args.review_queue,
                args.apply,
            )
            print(canonical_json(result))
    except NoInkReadError as exc:
        print(f"Decisions document stopped safely: {exc}", file=sys.stderr)
        if args.command == "process-archive":
            _send_pipeline_alert(
                args,
                "reMarkable Decisions: no ink could be read",
                (
                    f"{exc}\n\nManifest: {args.manifest}\nArchive: {args.archive}\n"
                    "The archive downloaded but its page order/ink could not be "
                    "resolved, so ZERO ink was read. Nothing was applied. Pull the "
                    "doc by hand (rmapi geta) and vision-read it as the fallback."
                ),
                "remarkable-no-ink",
            )
        return EXIT_NO_INK_READ
    except (OSError, json.JSONDecodeError, TodayPageError, subprocess.CalledProcessError) as exc:
        print(f"Decisions document stopped safely: {exc}", file=sys.stderr)
        if args.command == "process-archive":
            _send_pipeline_alert(
                args,
                "reMarkable Decisions pipeline stopped (input error)",
                (
                    f"{exc}\n\nManifest: {getattr(args, 'manifest', '?')}\n"
                    f"Archive: {getattr(args, 'archive', '?')}"
                ),
                "remarkable-input-error",
            )
        return EXIT_INPUT_ERROR
    except Exception as exc:  # pragma: no cover - genuine crash, never silent
        print(f"Decisions document crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.command == "process-archive":
            _send_pipeline_alert(
                args,
                "reMarkable Decisions pipeline crashed",
                (
                    f"{type(exc).__name__}: {exc}\n\n"
                    f"Manifest: {getattr(args, 'manifest', '?')}\n"
                    f"Archive: {getattr(args, 'archive', '?')}"
                ),
                "remarkable-crash",
            )
        return EXIT_CRASH
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
