"""Tests for card_premise_watch.py (mc-cxjp3, plus the cross-reviewer's cross-review fixes).

All fixtures are fake: a small manifest, a small REVIEW-QUEUE.md, a small
read doc, and a stubbed ticket-status lookup. No test calls the real `bd`
CLI or touches the real archive.
"""
from __future__ import annotations

import json
from pathlib import Path

import card_premise_watch as cpw

DATE = "2026-10-04"

ACTIVE_QUEUE = """# Review Queue

## Active - Blocking / Production

| Item | Project | Age | Ask |
|---|---|---|---|
| Thing one | example-project | 2026-10-04 | Some ask text. Row: card_one_row Read: docs/decisions/card-one.md |

## Closed

| Item | Project | Age | Ask |
|---|---|---|---|
"""

CLOSED_QUEUE = """# Review Queue

## Active - Blocking / Production

| Item | Project | Age | Ask |
|---|---|---|---|

## Closed

| Item | Project | Age | Ask |
|---|---|---|---|
| Thing one | example-project | 2026-10-04 | Decided. Row: card_one_row Read: docs/decisions/card-one.md |
"""


def make_manifest(
    *,
    read_sha256: str,
    ask: str = "Some ask text.",
    description: str = "More detail.",
    read_path: str = "docs/decisions/card-one.md",
) -> dict:
    return {
        "manifest_hash": "fixed-hash-for-test",
        "cards": [
            {
                "card_id": "card-001",
                "row_key": "card_one_row",
                "headline": "Thing one?",
                "ask": ask,
                "description": description,
                "read": {"path": read_path, "sha256": read_sha256},
            }
        ],
    }


def write_fixture(
    tmp_path: Path,
    *,
    queue_text: str = ACTIVE_QUEUE,
    ask: str = "Some ask text.",
    description: str = "More detail.",
    read_text: str = "Plain read doc text, no markers.\n",
) -> tuple[Path, Path, Path]:
    """Create manifest, read doc, and REVIEW-QUEUE.md under tmp_path.

    Returns (repo_root, manifest_path, review_queue_path).
    """
    repo_root = tmp_path
    (repo_root / "docs" / "decisions").mkdir(parents=True)
    read_doc = repo_root / "docs" / "decisions" / "card-one.md"
    read_doc.write_text(read_text, encoding="utf-8")
    read_sha256 = cpw.sha256_file(read_doc)

    manifest_path = repo_root / "decisions.manifest.json"
    manifest_path.write_text(
        json.dumps(
            make_manifest(read_sha256=read_sha256, ask=ask, description=description),
            indent=2,
        ),
        encoding="utf-8",
    )

    review_queue_path = repo_root / "REVIEW-QUEUE.md"
    review_queue_path.write_text(queue_text, encoding="utf-8")

    return repo_root, manifest_path, review_queue_path


def make_bd_lookup(status_box: dict[str, str]):
    def lookup(ticket_id: str) -> dict:
        if ticket_id in status_box:
            value = status_box[ticket_id]
            if isinstance(value, dict):
                return value
            return {"status": value, "id": ticket_id}
        return {"status": None, "error": f"no such ticket in stub: {ticket_id}"}

    return lookup


# ---------------------------------------------------------------------------
# Marker extraction unit tests
# ---------------------------------------------------------------------------


def test_extract_ticket_ids_dedupes_and_preserves_order():
    text = "see mc-aaaaa and mc-bbbbb.1 again mc-aaaaa"
    assert cpw.extract_ticket_ids(text) == ["mc-aaaaa", "mc-bbbbb.1"]


def test_active_row_keys_only_reads_active_sections():
    assert cpw.active_row_keys(ACTIVE_QUEUE) == {"card_one_row"}
    assert cpw.active_row_keys(CLOSED_QUEUE) == set()


def test_prose_mention_without_marker_is_not_a_link():
    card = {
        "ask": "This references mc-aaaaa in passing.",
        "description": "And mentions mc-bbbbb too, with no marker.",
        "read": {"path": "docs/decisions/card-one.md"},
    }
    assert cpw._card_ticket_ids(card, Path("/does-not-matter")) == []


def test_follow_up_line_in_read_doc_is_a_link(tmp_path):
    read_doc = tmp_path / "doc.md"
    read_doc.write_text("Some prose mentions mc-zzzzz earlier.\nFollow-up: mc-aaaaa\n", encoding="utf-8")
    card = {"ask": "", "description": "", "read": {"path": "doc.md"}}
    assert cpw._card_ticket_ids(card, tmp_path) == ["mc-aaaaa"]


def test_ticket_marker_in_ask_text_is_a_link():
    card = {
        "ask": "This is done. Ticket: mc-ddddd. Nothing else to say.",
        "description": "",
        "read": {},
    }
    assert cpw._card_ticket_ids(card, Path("/does-not-matter")) == ["mc-ddddd"]


# ---------------------------------------------------------------------------
# Dotted sub-ticket ids in markers (the cross-reviewer cross-review of 0fa137ed,
# point 1): a '.' inside an id must not be swallowed by a stop-at-period
# capture, and a '.' that genuinely ends the sentence must not be pulled
# into the id or used to merge unrelated ids together. Covered for both
# marker forms: the text marker (ask/description) and the line marker
# (read doc).
# ---------------------------------------------------------------------------


def test_dotted_id_single_in_text_marker():
    card = {"ask": "Ticket: mc-uy3b.1", "description": "", "read": {}}
    assert cpw._card_ticket_ids(card, Path("/does-not-matter")) == ["mc-uy3b.1"]


def test_dotted_id_list_with_and_in_text_marker():
    card = {
        "ask": "Tickets: mc-4u3ei.11 and mc-4u3ei.4 are the follow-ups for this.",
        "description": "",
        "read": {},
    }
    assert cpw._card_ticket_ids(card, Path("/does-not-matter")) == [
        "mc-4u3ei.11",
        "mc-4u3ei.4",
    ]


def test_dotted_id_list_with_commas_in_text_marker():
    card = {
        "ask": "Tickets: mc-aaaaa.1, mc-bbbbb.2 are the follow-ups.",
        "description": "",
        "read": {},
    }
    assert cpw._card_ticket_ids(card, Path("/does-not-matter")) == [
        "mc-aaaaa.1",
        "mc-bbbbb.2",
    ]


def test_dotted_id_trailing_sentence_period_in_text_marker():
    card = {
        "ask": "Ticket: mc-uy3b.1. Nothing else to say.",
        "description": "",
        "read": {},
    }
    assert cpw._card_ticket_ids(card, Path("/does-not-matter")) == ["mc-uy3b.1"]


def test_dotted_id_single_in_line_marker(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("Follow-up: mc-uy3b.1\n", encoding="utf-8")
    card = {"ask": "", "description": "", "read": {"path": "doc.md"}}
    assert cpw._card_ticket_ids(card, tmp_path) == ["mc-uy3b.1"]


def test_dotted_id_list_with_and_in_line_marker(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("Tickets: mc-4u3ei.11 and mc-4u3ei.4\n", encoding="utf-8")
    card = {"ask": "", "description": "", "read": {"path": "doc.md"}}
    assert cpw._card_ticket_ids(card, tmp_path) == ["mc-4u3ei.11", "mc-4u3ei.4"]


def test_dotted_id_list_with_commas_in_line_marker(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("Tickets: mc-aaaaa.1, mc-bbbbb.2\n", encoding="utf-8")
    card = {"ask": "", "description": "", "read": {"path": "doc.md"}}
    assert cpw._card_ticket_ids(card, tmp_path) == ["mc-aaaaa.1", "mc-bbbbb.2"]


def test_dotted_id_trailing_sentence_period_in_line_marker(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("Follow-up: mc-uy3b.1. Nothing else to say.\n", encoding="utf-8")
    card = {"ask": "", "description": "", "read": {"path": "doc.md"}}
    assert cpw._card_ticket_ids(card, tmp_path) == ["mc-uy3b.1"]


# ---------------------------------------------------------------------------
# End to end: snapshot then check
# ---------------------------------------------------------------------------


def test_clean_snapshot_then_check_is_not_stale(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Ticket: mc-aaaaa. The work is tracked there.",
        description="No further detail.",
        read_text="Plain read doc text.\n",
    )
    manifest = cpw._load_json(manifest_path)
    bd_lookup = make_bd_lookup({"mc-aaaaa": "open"})

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert result["stale"] == []
    assert result["unable"] == []
    assert result["no_linked_ticket"] == []


def test_row_moved_to_closed_is_stale(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(tmp_path)
    manifest = cpw._load_json(manifest_path)
    bd_lookup = make_bd_lookup({})

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )

    # Overnight, the row is answered and moved to Closed.
    review_queue_path.write_text(CLOSED_QUEUE, encoding="utf-8")

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert len(result["stale"]) == 1
    assert result["stale"][0]["row_key"] == "card_one_row"
    assert any("no longer in REVIEW-QUEUE.md Active" in r for r in result["stale"][0]["reasons"])


def test_read_doc_edited_is_stale(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path, read_text="Plain read doc text, version one.\n"
    )
    manifest = cpw._load_json(manifest_path)
    bd_lookup = make_bd_lookup({})

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )

    read_doc = repo_root / "docs" / "decisions" / "card-one.md"
    read_doc.write_text("Plain read doc text, version two, edited overnight.\n", encoding="utf-8")

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert len(result["stale"]) == 1
    assert any("sha256 changed" in r for r in result["stale"][0]["reasons"])


def test_historical_id_in_prose_does_not_cause_staleness(tmp_path):
    """A ticket id mentioned in plain prose, with no marker, is a historical
    reference, not a link. Its status changing must not mark the card stale
    (the cross-reviewer cross-review point 1: over-linking)."""
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="This references mc-aaaaa, a ticket from an earlier round.",
        description="No marker here either, just prose about mc-aaaaa.",
        read_text="The read doc also mentions mc-aaaaa in passing, no marker.\n",
    )
    manifest = cpw._load_json(manifest_path)
    status_box = {"mc-aaaaa": "open"}
    bd_lookup = make_bd_lookup(status_box)

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )

    # Overnight, mc-aaaaa (never actually linked) is closed.
    status_box["mc-aaaaa"] = "closed"

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert result["stale"] == []


def test_card_with_no_linked_ticket_is_reported_not_silently_clean(tmp_path):
    """A card with no explicit ticket link must show up as its own distinct
    status, not merge silently into 'clean' (the cross-reviewer cross-review point 2:
    under-linking)."""
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="No ticket is linked on this card at all.",
        description="Nothing to link here.",
        read_text="Nothing to link here either.\n",
    )
    manifest = cpw._load_json(manifest_path)
    bd_lookup = make_bd_lookup({})

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert snapshot["cards"][0]["has_linked_ticket"] is False

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert result["stale"] == []
    assert result["unable"] == []
    assert len(result["no_linked_ticket"]) == 1
    assert result["no_linked_ticket"][0]["note"] == cpw.NO_LINKED_TICKET_NOTE


def test_follow_up_line_linkage_is_stale_when_that_ticket_closes(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Some ask text with no marker.",
        description="No marker in description either.",
        read_text="Background prose.\nFollow-up: mc-ccccc\n",
    )
    manifest = cpw._load_json(manifest_path)
    status_box = {"mc-ccccc": "open"}
    bd_lookup = make_bd_lookup(status_box)

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert snapshot["cards"][0]["ticket_ids"] == ["mc-ccccc"]

    status_box["mc-ccccc"] = "closed"

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert len(result["stale"]) == 1
    assert "mc-ccccc" in result["stale"][0]["reasons"][0]


def test_ticket_marker_in_ask_is_stale_when_it_closes(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="This is done. Ticket: mc-ddddd. Nothing else to say.",
        description="No marker here.",
        read_text="No marker here either.\n",
    )
    manifest = cpw._load_json(manifest_path)
    status_box = {"mc-ddddd": "open"}
    bd_lookup = make_bd_lookup(status_box)

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert snapshot["cards"][0]["ticket_ids"] == ["mc-ddddd"]

    status_box["mc-ddddd"] = "closed"

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup
    )
    assert len(result["stale"]) == 1
    assert "mc-ddddd" in result["stale"][0]["reasons"][0]


def test_bd_lookup_failure_is_unable_to_check_not_stale(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Ticket: mc-eeeee is tracking this.",
        description="No further marker.",
        read_text="No marker here.\n",
    )
    manifest = cpw._load_json(manifest_path)
    status_box = {"mc-eeeee": "open"}
    bd_lookup_at_snapshot = make_bd_lookup(status_box)

    snapshot = cpw.build_snapshot(
        DATE, manifest, review_queue_path.read_text(encoding="utf-8"), repo_root, bd_lookup_at_snapshot
    )

    def failing_lookup(ticket_id: str) -> dict:
        return {"status": None, "error": f"bd show {ticket_id} exited 1: no such issue"}

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, failing_lookup
    )
    assert result["stale"] == []
    assert len(result["unable"]) == 1
    assert "mc-eeeee" in result["unable"][0]["unable_tickets"][0]


def test_missing_snapshot_file_exits_two_via_cli(tmp_path, capsys):
    repo_root, manifest_path, review_queue_path = write_fixture(tmp_path)
    archive_root = tmp_path / "archive"
    (archive_root / DATE).mkdir(parents=True)
    manifest_dest = archive_root / DATE / f"decisions-{DATE}.manifest.json"
    manifest_dest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    rc = cpw.main(
        [
            "--archive-root",
            str(archive_root),
            "--repo-root",
            str(repo_root),
            "--review-queue",
            str(review_queue_path),
            "check",
            "--date",
            DATE,
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "no snapshot" in captured.err.lower() or "no snapshot" in captured.out.lower()


def test_missing_manifest_exits_two_via_cli(tmp_path):
    repo_root, _manifest_path, review_queue_path = write_fixture(tmp_path)
    archive_root = tmp_path / "empty-archive"

    rc = cpw.main(
        [
            "--archive-root",
            str(archive_root),
            "--repo-root",
            str(repo_root),
            "--review-queue",
            str(review_queue_path),
            "check",
            "--date",
            DATE,
        ]
    )
    assert rc == 2


def test_cli_snapshot_then_check_round_trip_exits_zero(tmp_path, monkeypatch):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Ticket: mc-aaaaa tracks this.",
        description="No further marker.",
    )
    archive_root = tmp_path / "archive"
    (archive_root / DATE).mkdir(parents=True)
    manifest_dest = archive_root / DATE / f"decisions-{DATE}.manifest.json"
    manifest_dest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    status_box = {"mc-aaaaa": "open"}
    monkeypatch.setattr(
        cpw,
        "default_bd_lookup",
        lambda ticket_id, *, repo_root: make_bd_lookup(status_box)(ticket_id),
    )

    common = [
        "--archive-root",
        str(archive_root),
        "--repo-root",
        str(repo_root),
        "--review-queue",
        str(review_queue_path),
    ]

    rc_snapshot = cpw.main(common + ["snapshot", "--date", DATE])
    assert rc_snapshot == 0
    assert (archive_root / DATE / "card-premise-snapshot.json").exists()

    rc_check = cpw.main(common + ["check", "--date", DATE])
    assert rc_check == 0


def test_cli_check_exits_four_on_unable_to_check(tmp_path, monkeypatch):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Ticket: mc-aaaaa tracks this.",
        description="No further marker.",
    )
    archive_root = tmp_path / "archive"
    (archive_root / DATE).mkdir(parents=True)
    manifest_dest = archive_root / DATE / f"decisions-{DATE}.manifest.json"
    manifest_dest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    status_box = {"mc-aaaaa": "open"}
    monkeypatch.setattr(
        cpw,
        "default_bd_lookup",
        lambda ticket_id, *, repo_root: make_bd_lookup(status_box)(ticket_id),
    )

    common = [
        "--archive-root",
        str(archive_root),
        "--repo-root",
        str(repo_root),
        "--review-queue",
        str(review_queue_path),
    ]
    rc_snapshot = cpw.main(common + ["snapshot", "--date", DATE])
    assert rc_snapshot == 0

    # bd now fails for this ticket.
    monkeypatch.setattr(
        cpw,
        "default_bd_lookup",
        lambda ticket_id, *, repo_root: {"status": None, "error": "bd is down"},
    )

    rc_check = cpw.main(common + ["check", "--date", DATE])
    assert rc_check == 4


# ---------------------------------------------------------------------------
# Snapshot-time lookup failure (the cross-reviewer cross-review of 0fa137ed, point 2):
# a card whose linked ticket could not be read AT SNAPSHOT TIME must stay
# unable-to-check even if the later check-time lookup succeeds. There is no
# valid baseline to compare the current status against, so this must never
# read as clean.
# ---------------------------------------------------------------------------


def test_snapshot_time_lookup_failure_then_successful_check_is_unable_to_check(tmp_path):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Ticket: mc-eeeee tracks this.",
        description="No further marker.",
    )
    manifest = cpw._load_json(manifest_path)

    def failing_at_snapshot(ticket_id: str) -> dict:
        return {
            "status": None,
            "error": f"bd show {ticket_id} exited 1: no such issue (not yet created)",
        }

    snapshot = cpw.build_snapshot(
        DATE,
        manifest,
        review_queue_path.read_text(encoding="utf-8"),
        repo_root,
        failing_at_snapshot,
    )
    assert snapshot["cards"][0]["tickets"]["mc-eeeee"]["status"] is None

    def succeeding_at_check(ticket_id: str) -> dict:
        return {"status": "open", "id": ticket_id}

    result = cpw.check_snapshot(
        snapshot, review_queue_path.read_text(encoding="utf-8"), repo_root, succeeding_at_check
    )
    assert result["stale"] == []
    assert len(result["unable"]) == 1
    assert "mc-eeeee" in result["unable"][0]["unable_tickets"][0]
    assert "snapshot lookup failed" in result["unable"][0]["unable_tickets"][0]


def test_cli_check_exits_four_when_snapshot_time_lookup_failed_even_if_check_time_succeeds(
    tmp_path, monkeypatch, capsys
):
    repo_root, manifest_path, review_queue_path = write_fixture(
        tmp_path,
        ask="Ticket: mc-aaaaa tracks this.",
        description="No further marker.",
    )
    archive_root = tmp_path / "archive"
    (archive_root / DATE).mkdir(parents=True)
    manifest_dest = archive_root / DATE / f"decisions-{DATE}.manifest.json"
    manifest_dest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    # bd fails during snapshot.
    monkeypatch.setattr(
        cpw,
        "default_bd_lookup",
        lambda ticket_id, *, repo_root: {"status": None, "error": "bd is down"},
    )

    common = [
        "--archive-root",
        str(archive_root),
        "--repo-root",
        str(repo_root),
        "--review-queue",
        str(review_queue_path),
    ]
    rc_snapshot = cpw.main(common + ["snapshot", "--date", DATE])
    assert rc_snapshot == 0
    snapshot_captured = capsys.readouterr()
    assert "UNABLE TO CHECK mc-aaaaa" in snapshot_captured.out

    # bd recovers by check time.
    monkeypatch.setattr(
        cpw,
        "default_bd_lookup",
        lambda ticket_id, *, repo_root: {"status": "open", "id": ticket_id},
    )

    rc_check = cpw.main(common + ["check", "--date", DATE])
    assert rc_check == 4
    check_captured = capsys.readouterr()
    assert "UNABLE TO CHECK" in check_captured.out
    assert "mc-aaaaa" in check_captured.out
