"""Tests for remarkable/consume_decisions.py.

The review queue's own parser is the acceptance test: after any write the file
must still parse through today_page.parse_review_queue, and the active row
count must drop by exactly the number of rows closed. Nothing here ever writes
to a real queue file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "remarkable"))

import consume_decisions as cd  # noqa: E402
import today_page as tp  # noqa: E402

REAL_QUEUE = ROOT / "REVIEW-QUEUE.md"


# ---------------------------------------------------------------------------
# Fixture builders (real files on disk, no mocks)
# ---------------------------------------------------------------------------


def item_id_for(title: str, project: str, age: str) -> str:
    return hashlib.sha256(f"{title}\0{project}\0{age}".encode()).hexdigest()[:12]


def ask_for(options: list[str]) -> str:
    numbered = " ".join(
        f"({n + 1}:legacy_{n + 1}) {text}" for n, text in enumerate(options)
    )
    return (
        "Some background about the thing. It needs a choice. "
        "Recommendation: do the first one. "
        f"Decide: {numbered}"
    )


def write_queue(
    path: Path, rows: list[dict[str, str]], drift_closed_table: bool = False
) -> None:
    """Write a queue fixture.

    drift_closed_table reproduces the 2026-08-19 defect: the Closed table's
    header and separator sit far below the heading instead of directly under
    it, so today_page.close_review_queue_row cannot find where to insert.
    """
    lines = [
        "# Review Queue",
        "",
        "## Active - Blocking",
        "",
        "| Item | Project | Age | Ask |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['title']} | {row['project']} | {row['age']} | {row['ask']} |"
        )
    lines += ["", "## Closed", ""]
    if drift_closed_table:
        lines += ["| An older thing | 2026-08-01 | Already handled. |"]
        lines += ["" for _ in range(10)]
        lines += ["| Item | Closed | Disposition |", "|---|---|---|", ""]
    else:
        lines += [
            "| Item | Closed | Disposition |",
            "|---|---|---|",
            "| An older thing | 2026-08-01 | Already handled. |",
            "",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_document(
    base: Path,
    date: str,
    cards: list[dict],
    ambiguities: list[dict] | None = None,
    write_manifest: bool = True,
) -> Path:
    """Write an archive folder that looks like a real processed document."""
    page_id = f"decisions-{date}-fixture"
    folder = base / date
    processed = folder / "processed"
    processed.mkdir(parents=True, exist_ok=True)

    decisions: list[dict] = []
    entries: list[dict] = []
    manifest_cards: list[dict] = []
    for card_index, card in enumerate(cards, start=1):
        card_id = card.get("card_id", f"card-{card_index:03d}")
        manifest_cards.append(
            {
                "card_id": card_id,
                "item_id": card["item_id"],
                "headline": card["headline"],
                "page": 1,
            }
        )
        for number, (label, choice) in enumerate(card["options"], start=1):
            entry_id = f"decision-{card_index:03d}{chr(96 + number)}"
            entry = {
                "entry_id": entry_id,
                "kind": "decision",
                "item_id": card["item_id"],
                "card_id": card_id,
                "decision_number": number,
                "label": label,
                "page": 1,
            }
            entries.append(entry)
            if choice is None:
                continue
            decisions.append(
                {
                    "entry_id": entry_id,
                    "item_id": card["item_id"],
                    "card_id": card_id,
                    "decision_number": number,
                    "label": label,
                    "choice": choice,
                    "page": 1,
                    "stroke_ids": [f"page-1-line-{len(decisions) + 1}"],
                }
            )

    resolution = {
        "schema_version": tp.LEGACY_SCHEMA_VERSION,
        "layout_version": tp.LEGACY_LAYOUT_VERSION,
        "page_id": page_id,
        "ink_hash": "fixture",
        "status": "needs_review" if ambiguities else "resolved",
        "actions": [],
        "decisions": decisions,
        "ambiguities": ambiguities or [],
        "ignored_strokes": [],
        "note_regions": [],
    }
    (processed / "resolution.json").write_text(
        json.dumps(resolution, indent=2), encoding="utf-8"
    )
    if write_manifest:
        manifest = {
            "schema_version": tp.LEGACY_SCHEMA_VERSION,
            "layout_version": tp.LEGACY_LAYOUT_VERSION,
            "page_id": page_id,
            "date": date,
            "page_count": 1,
            "entries": entries,
            "cards": manifest_cards,
        }
        (folder / f"decisions-{date}.manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    return processed


@pytest.fixture
def workspace(tmp_path: Path):
    """A temp queue, receipts dir, and archive root. Never the real repo files."""
    queue = tmp_path / "REVIEW-QUEUE.md"
    receipts = tmp_path / "receipts"
    archive = tmp_path / "today-pages"
    archive.mkdir()
    return {"queue": queue, "receipts": receipts, "archive": archive, "root": tmp_path}


def run(workspace, processed_dirs, apply=False):
    return cd.run(
        processed_dirs,
        workspace["queue"],
        workspace["receipts"],
        apply=apply,
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_single_option_yes_row_closes(workspace):
    title = "Ship the thing (mc-0001)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Ship it now?", "Wait a week?"]),
            },
            {
                "title": "An untouched row",
                "project": "other",
                "age": "2026-08-11",
                "ask": ask_for(["Leave this alone?"]),
            },
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Ship it now?", "YES")],
            }
        ],
    )

    results, code = run(workspace, [processed], apply=True)
    result = results[0]

    assert code == cd.EXIT_OK
    assert len(result.recorded) == 1
    assert result.held == []

    rows = tp.parse_review_queue(workspace["queue"], require_keys=False)
    assert len(rows) == 1
    assert rows[0]["title"] == "An untouched row"

    text = workspace["queue"].read_text(encoding="utf-8")
    active_text, closed_text = text.split("## Closed", 1)
    assert title not in active_text.split("## Pen dispositions")[0]
    assert title in closed_text
    assert "PEN 2026-08-16" in closed_text
    assert 'YES "Ship it now?"' in closed_text
    assert "## Pen dispositions - 2026-08-16" in text
    assert "- **YES** Ship it now? - row: " + title in text


def test_multi_option_row_records_every_answer(workspace):
    title = "Correct the four records (mc-0002)?"
    project, age = "field-app", "2026-08-12"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Correct all four?", "Records only?", "Leave it?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [
                    ("Correct all four?", "YES"),
                    ("Records only?", "NO"),
                    ("Leave it?", "NO"),
                ],
            }
        ],
    )

    results, code = run(workspace, [processed], apply=True)
    plan = results[0].recorded[0]

    assert code == cd.EXIT_OK
    assert [answer["choice"] for answer in plan.answers] == ["YES", "NO", "NO"]
    text = workspace["queue"].read_text(encoding="utf-8")
    for line in [
        "- **YES** Correct all four? - row: " + title,
        "- **NO** Records only? - row: " + title,
        "- **NO** Leave it? - row: " + title,
    ]:
        assert line in text
    assert tp.parse_review_queue(workspace["queue"], require_keys=False) == []


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------


def test_multiple_yes_on_one_card_records_cleanly(workspace):
    """Cards present cumulative options, not always exclusive ones.

    On the real 2026-08-16 document card-001 asked "Clear the old test copies?"
    and "Those and the duplicate model files?", and card-009 asked "One-time
    refresh now?" and "Also authorize the scheduler design?" while "Accept
    staleness through the week?" was marked NO. Two YES marks on one card is a
    coherent answer. Exclusivity is never inferred from card structure.
    """
    title = "Two yeses on one card (mc-0003)?"
    project, age = "example-project", "2026-08-13"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["One-time refresh now?", "Also the scheduler?",
                                "Accept staleness?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [
                    ("One-time refresh now?", "YES"),
                    ("Also the scheduler?", "YES"),
                    ("Accept staleness?", "NO"),
                ],
            }
        ],
    )

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_OK
    assert results[0].held == []
    assert len(results[0].recorded) == 1
    assert [a["choice"] for a in results[0].recorded[0].answers] == ["YES", "YES", "NO"]
    text = workspace["queue"].read_text(encoding="utf-8")
    assert "- **YES** One-time refresh now? - row: " + title in text
    assert "- **YES** Also the scheduler? - row: " + title in text
    assert "- **NO** Accept staleness? - row: " + title in text
    assert tp.parse_review_queue(workspace["queue"], require_keys=False) == []


def test_one_option_answered_both_ways_holds(workspace):
    """The only real contradiction: a single entry carrying YES and NO."""
    title = "Both marks on one option (mc-0014)?"
    project, age = "example-project", "2026-08-13"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Option one?", "Option two?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Option one?", "YES"), ("Option two?", "NO")],
            }
        ],
    )
    # Forge the stored resolution so the SAME entry carries both marks, the way
    # a resolution written before multiple_choices detection could look.
    resolution_file = processed / "resolution.json"
    resolution = json.loads(resolution_file.read_text(encoding="utf-8"))
    duplicate = dict(resolution["decisions"][0])
    duplicate["choice"] = "NO"
    resolution["decisions"].append(duplicate)
    resolution_file.write_text(json.dumps(resolution, indent=2), encoding="utf-8")
    # No strokes.json here, so the stored resolution is what gets consumed.
    assert not (processed / "strokes.json").exists()

    results, code = run(workspace, [processed], apply=True)

    # An entry that appears twice fails the WHOLE document closed at
    # validation, before any planning: a resolution that contradicts itself is
    # not a trustworthy artifact, so none of it is recorded.
    assert code == cd.EXIT_ERROR
    assert results[0].recorded == []
    assert results[0].plans == []
    assert "appears more than once" in results[0].error
    assert len(tp.parse_review_queue(workspace["queue"], require_keys=False)) == 1
    assert "## Pen dispositions" not in workspace["queue"].read_text(encoding="utf-8")


def test_one_option_answered_both_ways_holds_when_validation_cannot_see_it(workspace):
    """The row-level guard still catches a both-ways answer that slips through."""
    answers = [
        {"entry_id": "decision-001a", "choice": "YES", "label": "Go?"},
        {"entry_id": "decision-001a", "choice": "NO", "label": "Go?"},
    ]
    assert cd._answered_both_ways(answers) is True
    assert cd._answered_both_ways(answers[:1]) is False


def test_row_no_longer_in_active_holds(workspace):
    write_queue(
        workspace["queue"],
        [
            {
                "title": "A row whose wording changed since the sheet",
                "project": "example-project",
                "age": "2026-08-18",
                "ask": ask_for(["Do it?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": "deadbeef0000",
                "headline": "The row as it was worded on the sheet (mc-0004)?",
                "options": [("Do it?", "YES")],
            }
        ],
    )

    results, code = run(workspace, [processed], apply=True)
    plan = results[0].held[0]

    assert code == cd.EXIT_HOLD
    assert plan.hold_reasons == ["row_not_in_active"]
    assert plan.title == "The row as it was worded on the sheet (mc-0004)?"
    assert len(tp.parse_review_queue(workspace["queue"], require_keys=False)) == 1


def test_ambiguity_touching_the_row_holds_it(workspace):
    clean_title = "A clean row (mc-0005)?"
    murky_title = "A row with a smudged mark (mc-0006)?"
    project, age = "example-project", "2026-08-14"
    write_queue(
        workspace["queue"],
        [
            {
                "title": clean_title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            },
            {
                "title": murky_title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            },
        ],
    )
    murky_item = item_id_for(murky_title, project, age)
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(clean_title, project, age),
                "headline": clean_title,
                "options": [("Go?", "YES")],
            },
            {
                "item_id": murky_item,
                "headline": murky_title,
                "options": [("Go?", "YES")],
            },
        ],
        ambiguities=[
            {
                "stroke_id": "page-1-line-9",
                "reason": "mark_crosses_boxes",
                "page": 1,
                "candidate_entry_ids": ["decision-002a"],
            }
        ],
    )

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert [plan.title for plan in results[0].recorded] == [clean_title]
    assert [plan.title for plan in results[0].held] == [murky_title]
    assert results[0].held[0].hold_reasons == ["ambiguity_touches_row"]

    rows = tp.parse_review_queue(workspace["queue"], require_keys=False)
    assert [row["title"] for row in rows] == [murky_title]


def test_unattributable_ambiguity_without_a_manifest_holds_everything(workspace):
    title = "No manifest on disk (mc-0007)?"
    project, age = "example-project", "2026-08-15"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
        ambiguities=[
            {"stroke_id": "s1", "reason": "mark_too_large", "page": 1},
        ],
        write_manifest=False,
    )

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert results[0].held[0].hold_reasons == ["document_blocked"]
    assert len(results[0].orphan_ambiguities) == 1
    assert len(tp.parse_review_queue(workspace["queue"], require_keys=False)) == 1


# ---------------------------------------------------------------------------
# Idempotence and file safety
# ---------------------------------------------------------------------------


def test_rerun_is_a_no_op_and_says_so(workspace):
    title = "Run me twice (mc-0008)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
    )

    first, first_code = run(workspace, [processed], apply=True)
    assert first_code == cd.EXIT_OK
    assert len(first[0].recorded) == 1
    receipts = sorted(workspace["receipts"].glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["item_id"] == item_id_for(title, project, age)
    assert receipt["answers"][0]["choice"] == "YES"

    after_first = workspace["queue"].read_bytes()

    second, second_code = run(workspace, [processed], apply=True)
    assert second_code == cd.EXIT_OK
    assert second[0].recorded == []
    assert len(second[0].already) == 1
    assert workspace["queue"].read_bytes() == after_first
    assert len(sorted(workspace["receipts"].glob("*.json"))) == 1

    report = cd.render_report(second, apply=True)
    assert "NOOP" in report
    assert "already recorded" in report


def test_write_is_discarded_when_the_queue_would_not_parse(workspace, monkeypatch):
    title = "Guard rail (mc-0009)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            },
            {
                "title": "A bystander row that must survive",
                "project": "other",
                "age": "2026-08-11",
                "ask": ask_for(["Leave this alone?"]),
            },
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
    )
    before = workspace["queue"].read_bytes()

    real_insert = cd.insert_disposition_section

    def corrupt(path, document, plans):
        real_insert(path, document, plans)
        path.write_text("## Active - Blocking\n| broken |\n", encoding="utf-8")

    monkeypatch.setattr(cd, "insert_disposition_section", corrupt)

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_ERROR
    assert results[0].error
    assert workspace["queue"].read_bytes() == before
    assert list(workspace["receipts"].glob("*.json")) == [] or not workspace[
        "receipts"
    ].exists()


def test_dry_run_changes_nothing(workspace):
    title = "Look but do not touch (mc-0010)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
    )
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=False)

    assert code == cd.EXIT_OK
    assert len(results[0].recorded) == 1
    assert workspace["queue"].read_bytes() == before
    assert not workspace["receipts"].exists()


def test_no_decisions_is_reported_explicitly(workspace):
    write_queue(
        workspace["queue"],
        [
            {
                "title": "Nothing was answered (mc-0011)?",
                "project": "example-project",
                "age": "2026-08-10",
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": "aaaaaaaaaaaa",
                "headline": "Unanswered card",
                "options": [("Go?", None)],
            }
        ],
    )

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_OK
    assert results[0].plans == []
    report = cd.render_report(results, apply=True)
    assert "no decisions found" in report


# ---------------------------------------------------------------------------
# The real 2026-08-16 document against a copy of the real queue
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The bus alert (argument order matches tools/monitoring/bus-alert.sh usage:
# "subject" "body" [idempotency-slug])
# ---------------------------------------------------------------------------


def test_send_alert_passes_subject_body_slug_in_that_order(tmp_path, monkeypatch):
    helper = tmp_path / "fake-bus-alert.sh"
    record = tmp_path / "args.txt"
    helper.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$@\" > " + str(record) + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setattr(cd, "BUS_ALERT", helper)

    cd.send_alert("a subject", "a body", "a-slug")

    assert record.read_text(encoding="utf-8").splitlines() == [
        "a subject",
        "a body",
        "a-slug",
    ]


def test_send_alert_raises_when_the_helper_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "BUS_ALERT", tmp_path / "not-there.sh")
    with pytest.raises(cd.ConsumeError) as excinfo:
        cd.send_alert("s", "b")
    assert "missing" in str(excinfo.value)


def test_send_alert_raises_when_the_helper_fails(tmp_path, monkeypatch):
    helper = tmp_path / "failing.sh"
    helper.write_text("#!/bin/bash\necho boom >&2\nexit 3\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setattr(cd, "BUS_ALERT", helper)
    with pytest.raises(cd.ConsumeError) as excinfo:
        cd.send_alert("s", "b")
    assert "exit 3" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_cli_apply_exits_nonzero_when_the_alert_fails(workspace, tmp_path, monkeypatch):
    title = "Alert path (mc-0012)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
    )
    monkeypatch.setattr(cd, "BUS_ALERT", tmp_path / "gone.sh")

    code = cd.main(
        [
            "--processed-dir",
            str(processed),
            "--review-queue",
            str(workspace["queue"]),
            "--receipts-dir",
            str(workspace["receipts"]),
            "--apply",
        ]
    )
    assert code == cd.EXIT_ERROR


def test_cli_without_a_target_is_an_error():
    assert cd.main([]) == cd.EXIT_ERROR


# ---------------------------------------------------------------------------
# The Closed table's structure (mc-63x2 drift alarm)
#
# On 2026-08-19 the Closed table's header and separator had drifted 98 lines
# below the "## Closed" heading. today_page.close_review_queue_row only looks
# for the separator within 8 lines of the heading, so from the moment of the
# drift NO row could ever be closed by a pen approval, and nothing said so.
# These two tests make that drift fail loudly instead.
# ---------------------------------------------------------------------------


def closed_table_shape(text: str) -> list[str]:
    """The first two non-blank lines under the first '## Closed' heading."""
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("## Closed")),
        None,
    )
    assert start is not None, "the review queue has no Closed section"
    return [line for line in lines[start + 1 :] if line.strip()][:2]


def test_drifted_closed_table_holds_loudly_instead_of_recording(workspace):
    title = "Drifted Closed table (mc-0013)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask_for(["Go?"]),
            }
        ],
        drift_closed_table=True,
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
    )
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert results[0].recorded == []
    reason = results[0].held[0].hold_text()
    assert "could not be closed" in reason
    assert "Closed table could not be found" in reason
    # The answer is not silently dropped and the row is not silently kept clean:
    # the queue is untouched and the run reports the hold.
    assert workspace["queue"].read_bytes() == before
    assert len(tp.parse_review_queue(workspace["queue"], require_keys=False)) == 1
    assert "HOLD" in cd.render_report(results, apply=True)


# ---------------------------------------------------------------------------
# Re-resolving the ink (mc-63x2 defect: stale resolution.json)
#
# The resolution.json files in the archive were written by whichever resolver
# was current on the day. The ones predating the mc-rrjv adjacent-box fix carry
# ambiguities the current resolver does not produce. Trusting the file holds
# answers that are not ambiguous at all, so the consumer replays the ink.
# ---------------------------------------------------------------------------


def tick(bounds, stroke_id="tick", page=1, inset=2):
    """A check mark inside a box, the shape today_page's own tests use."""
    x0, y0, x1, y1 = bounds
    return {
        "stroke_id": stroke_id,
        "page": page,
        "points": [
            [x0 + inset, y0 + inset],
            [(x0 + x1) / 2, y1 - inset],
            [x1 - inset, y0 + inset],
        ],
    }


def build_legacy_manifest(date: str, rows: list[dict]) -> dict:
    """Build a v2 replay fixture without making the current generator emit v2."""
    manifest = tp.build_manifest(
        date, [], rows, {"review_queue": "fixture"}, require_keys=False
    )
    manifest["schema_version"] = tp.LEGACY_SCHEMA_VERSION
    manifest["layout_version"] = tp.LEGACY_LAYOUT_VERSION
    answer_pages = sorted({entry["page"] for entry in manifest["entries"]})
    page_map = {page: index for index, page in enumerate(answer_pages, start=1)}
    for entry in manifest["entries"]:
        entry["page"] = page_map[entry["page"]]
    for card in manifest["cards"]:
        card["page"] = page_map[card["page"]]
        layout = card["layout"]
        layout["page"] = card["page"]
        note_top = min(layout["top"], layout["bottom"] + 48.0)
        layout["write_in_bbox"] = [
            layout["x"],
            layout["bottom"],
            layout["x"] + layout["width"],
            note_top,
        ]
        layout["write_in_lines"] = [
            [layout["x"] + 2, note_top - 10, layout["x"] + layout["width"] - 2, note_top - 10],
            [layout["x"] + 2, note_top - 30, layout["x"] + layout["width"] - 2, note_top - 30],
        ]
        layout["write_in_line"] = layout["write_in_lines"][0]
    manifest["appendices"] = []
    manifest["answerable_page_count"] = len(answer_pages)
    manifest["page_count"] = len(answer_pages)
    manifest["manifest_hash"] = tp.manifest_digest(manifest)
    tp.validate_manifest(manifest)
    return manifest


def build_real_ink_document(
    base: Path, date: str, queue: Path, stale_ambiguity: bool = True
) -> tuple[Path, str]:
    """A real manifest and real ink, plus a deliberately stale resolution.json.

    The manifest comes from today_page.build_manifest against a real queue, the
    strokes are real ticks in real boxes, and the stored resolution is what the
    current resolver produces PLUS a fabricated ambiguity of the kind the old
    resolver emitted. A consumer that trusts the file holds; one that replays
    the ink records.
    """
    rows = tp.parse_review_queue(queue, require_keys=False)
    manifest = build_legacy_manifest(date, rows)
    entry = next(e for e in manifest["entries"] if e["kind"] == "decision")
    yes_box = next(box for box in entry["boxes"] if box["choice"] == "YES")
    strokes = {
        "schema_version": manifest["schema_version"],
        "layout_version": manifest["layout_version"],
        "page_id": manifest["page_id"],
        "strokes": [tick(yes_box["bbox"], page=entry["page"])],
    }
    fresh = tp.resolve(manifest, strokes)
    assert len(fresh["decisions"]) == 1, "the fixture tick did not land in a box"
    assert fresh["ambiguities"] == []

    folder = base / date
    processed = folder / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (folder / f"decisions-{date}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (processed / "strokes.json").write_text(
        json.dumps(strokes, indent=2), encoding="utf-8"
    )
    stored = json.loads(json.dumps(fresh))
    if stale_ambiguity:
        stored["status"] = "needs_review"
        stored["ambiguities"] = [
            {
                "stroke_id": "tick",
                "reason": "mark_crosses_boxes",
                "page": entry["page"],
                "candidate_entry_ids": [entry["entry_id"]],
            }
        ]
    (processed / "resolution.json").write_text(
        json.dumps(stored, indent=2), encoding="utf-8"
    )
    return processed, entry["item_id"]


@pytest.fixture
def real_ink_queue(workspace):
    write_queue(
        workspace["queue"],
        [
            {
                "title": "Replay the ink (mc-0015)?",
                "project": "example-project",
                "age": "2026-08-10",
                "ask": ask_for(["Go?", "Wait?"]),
            }
        ],
    )
    return workspace


def test_stale_ambiguity_is_ignored_because_the_ink_is_re_resolved(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"]
    )

    results, code = run(workspace, [processed], apply=False)
    document = results[0].document

    assert document.resolution_source == "re-resolved"
    assert len(document.stored_resolution["ambiguities"]) == 1
    assert document.ambiguities == []
    assert code == cd.EXIT_OK
    assert len(results[0].recorded) == 1
    assert results[0].held == []
    assert "re-resolved with the current resolver" in cd.render_report(results, False)


def test_missing_strokes_falls_back_to_the_stored_resolution_and_says_so(
    real_ink_queue,
):
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"]
    )
    (processed / "strokes.json").unlink()

    results, code = run(workspace, [processed], apply=False)
    document = results[0].document

    assert document.resolution_source == "stored"
    assert "no strokes.json" in document.resolution_note
    # The stale ambiguity is honoured when it cannot be re-checked: hold, never guess.
    assert code == cd.EXIT_HOLD
    assert results[0].recorded == []
    assert results[0].held[0].hold_reasons == ["ambiguity_touches_row"]
    report = cd.render_report(results, False)
    assert "STORED RESOLUTION USED" in report
    assert "no strokes.json" in report


def test_apply_saves_the_refreshed_resolution_beside_the_original(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"]
    )
    original = (processed / "resolution.json").read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_OK
    saved = results[0].reresolved_path
    assert saved is not None and saved.exists()
    assert saved.name.startswith("resolution-reresolved-")
    assert saved.parent == processed
    # The original record survives untouched.
    assert (processed / "resolution.json").read_bytes() == original
    refreshed = json.loads(saved.read_text(encoding="utf-8"))
    assert refreshed["ambiguities"] == []
    assert refreshed["reresolved_by"] == "consume_decisions"
    assert refreshed["reresolved_from"].endswith("resolution.json")


def test_dry_run_writes_no_refreshed_resolution(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"]
    )

    results, code = run(workspace, [processed], apply=False)

    assert code == cd.EXIT_OK
    assert results[0].reresolved_path is None
    assert list(processed.glob("resolution-reresolved-*.json")) == []


# ---------------------------------------------------------------------------
# The cross-reviewer's cross-review findings (BLOCK on be242027). One section per finding,
# each driving the production functions rather than a stand-in.
# ---------------------------------------------------------------------------


def one_row_document(workspace, title="Row under review (mc-r1)?", choice="YES",
                     ask_options=("Go?",), ambiguities=None, ask=None):
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": project,
                "age": age,
                "ask": ask or ask_for(list(ask_options)),
            }
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, project, age),
                "headline": title,
                "options": [(ask_options[0], choice)],
            }
        ],
        ambiguities=ambiguities,
    )
    return processed, item_id_for(title, project, age)


# --- Finding 1: concurrent writers ----------------------------------------


def test_a_concurrent_write_is_detected_and_the_queue_is_not_overwritten(
    workspace, monkeypatch
):
    """A writer that lands between our read and our write must not be lost."""
    processed, _ = one_row_document(workspace)
    intruder = "\n## Active - Added by someone else\n\n" \
               "| Item | Project | Age | Ask |\n|---|---|---|---|\n" \
               "| A row added mid-run | other | 2026-08-19 | " \
               "Some background. It needs a choice. Recommendation: do it. " \
               "Decide: (1:go) Go? |\n"

    real_commit = cd.commit_queue
    fired = {"count": 0}

    def racing_commit(queue_path, new_text, expected_sha):
        if fired["count"] == 0:
            fired["count"] = 1
            # Another writer appends to the live file after we read it.
            queue_path.write_text(
                queue_path.read_text(encoding="utf-8") + intruder, encoding="utf-8"
            )
        return real_commit(queue_path, new_text, expected_sha)

    monkeypatch.setattr(cd, "commit_queue", racing_commit)
    before = workspace["queue"].read_text(encoding="utf-8")

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_ERROR
    assert "changed while this run was working" in results[0].error
    after = workspace["queue"].read_text(encoding="utf-8")
    # The intruder's write survives; ours was abandoned.
    assert after == before + intruder
    assert "## Pen dispositions" not in after
    assert "A row added mid-run" in after


def test_commit_queue_refuses_a_stale_expected_hash(tmp_path):
    queue = tmp_path / "REVIEW-QUEUE.md"
    queue.write_text("original\n", encoding="utf-8")
    stale = cd.tp.sha256_bytes(b"something else\n")
    with pytest.raises(cd.QueueChangedError):
        cd.commit_queue(queue, "new\n", stale)
    assert queue.read_text(encoding="utf-8") == "original\n"


def test_queue_lock_is_exclusive_and_times_out(tmp_path):
    queue = tmp_path / "REVIEW-QUEUE.md"
    queue.write_text("x\n", encoding="utf-8")
    with cd.queue_lock(queue) as lock_path:
        assert lock_path.exists()
        with pytest.raises(cd.ConsumeError) as excinfo:
            # A second acquisition from a separate process must not get in.
            code = (
                "import sys; sys.path.insert(0, %r); import consume_decisions as cd;"
                "from pathlib import Path;"
                "cd.queue_lock(Path(%r), timeout=0.2).__enter__()"
                % (str(ROOT / "remarkable"), str(queue))
            )
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True
            )
            if proc.returncode != 0:
                raise cd.ConsumeError(proc.stderr)
        assert "another writer has held" in str(excinfo.value)


# --- Finding 2: a pipe in the Ask -----------------------------------------


@pytest.mark.parametrize(
    "ask",
    [
        "Background | with one pipe. It needs a choice. "
        "Recommendation: do it. Decide: (1:go) Go?",
        "Background | with | several | pipes. It needs a choice. "
        "Recommendation: do it. Decide: (1:go) Go?",
    ],
    ids=["one-pipe", "many-pipes"],
)
def test_an_ask_containing_pipes_is_preserved_byte_for_byte(workspace, ask):
    """Preserved, and preserved somewhere a pipe cannot forge a fourth cell.

    Updated by the round-2 review: the Ask used to be written raw into the
    three-column Closed table, so one pipe made a four-cell row that
    tp._split_row read as a different disposition. The bytes now live in the
    Pen dispositions section, outside any table, and the table cell carries an
    escaped copy that renders identically.
    """
    processed, _ = one_row_document(workspace, ask=ask)

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_OK
    assert len(results[0].recorded) == 1
    text = workspace["queue"].read_text(encoding="utf-8")
    assert f"original ask, as written: {ask}" in text
    closed = text.split("## Closed", 1)[1]
    assert f"Original ask: {tp.escape_table_cell(ask)}" in closed
    for line in closed.splitlines():
        cells = tp._split_row(line)
        if cells is not None:
            assert len(cells) == 3


# --- Finding 3: unvalidated resolution content -----------------------------


def _forge(processed: Path, mutate) -> None:
    path = processed / "resolution.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Remove the ink so the forged file is what actually gets consumed.
    (processed / "strokes.json").unlink(missing_ok=True)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d["decisions"][0].__setitem__("choice", "MAYBE"), "MAYBE"),
        (lambda d: d["decisions"][0].__setitem__("choice", ""), "''"),
        (lambda d: d["decisions"][0].pop("label"), "no label"),
        (lambda d: d["decisions"][0].__setitem__("entry_id", "decision-999z"),
         "not a decision entry in this page's manifest"),
        (lambda d: d["decisions"][0].__setitem__("label", "A different question?"),
         "disagrees with the manifest on label"),
        (lambda d: d["decisions"][0].__setitem__("item_id", "0000deadbeef"),
         "disagrees with the manifest on item_id"),
    ],
    ids=[
        "choice-maybe", "choice-empty", "missing-label", "unknown-entry",
        "label-skew", "item-skew",
    ],
)
def test_an_untrustworthy_resolution_fails_the_document_closed(
    workspace, mutate, expected
):
    processed, _ = one_row_document(workspace)
    _forge(processed, mutate)
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_ERROR
    assert results[0].plans == []
    assert results[0].recorded == []
    assert expected in results[0].error
    assert workspace["queue"].read_bytes() == before
    assert len(tp.parse_review_queue(workspace["queue"], require_keys=False)) == 1


# --- Finding 4: ambiguity must be fail-closed ------------------------------


@pytest.mark.parametrize(
    "ambiguity",
    [
        {"stroke_id": "s", "reason": "mark_crosses_boxes", "page": 1,
         "candidate_entry_ids": ["decision-999z"]},
        {"stroke_id": "s", "reason": "mark_crosses_boxes", "page": 1,
         "candidate_entry_ids": []},
        {"stroke_id": "s", "reason": "mark_too_large", "page": 1},
        {"stroke_id": "s", "reason": "multiple_choices", "page": 1,
         "entry_id": "decision-999z"},
        {"stroke_id": "s", "reason": "mark_crosses_boxes", "page": 1,
         "candidate_entry_ids": ["decision-001a", "decision-999z"]},
    ],
    ids=["unknown-entry", "empty-candidates", "no-candidate-key",
         "unknown-multiple-choices", "partial-skew"],
)
def test_an_unattributable_mark_blocks_the_whole_document(workspace, ambiguity):
    processed, _ = one_row_document(workspace, ambiguities=[ambiguity])
    # The forged ambiguity only exists in the stored file, so consume it.
    (processed / "strokes.json").unlink(missing_ok=True)
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert results[0].recorded == []
    assert len(results[0].orphan_ambiguities) == 1
    assert results[0].held[0].hold_reasons[0] == "document_blocked"
    assert workspace["queue"].read_bytes() == before
    assert "DOCUMENT BLOCKED" in cd.render_report(results, apply=True)


def test_an_attributable_mark_holds_only_its_own_row(workspace):
    """The block is for marks nobody can place, not for every mark."""
    clean, murky = "A clean row (mc-r2)?", "A smudged row (mc-r3)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {"title": clean, "project": project, "age": age, "ask": ask_for(["Go?"])},
            {"title": murky, "project": project, "age": age, "ask": ask_for(["Go?"])},
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {"item_id": item_id_for(clean, project, age), "headline": clean,
             "options": [("Go?", "YES")]},
            {"item_id": item_id_for(murky, project, age), "headline": murky,
             "options": [("Go?", "YES")]},
        ],
        ambiguities=[{"stroke_id": "s", "reason": "mark_crosses_boxes", "page": 1,
                      "candidate_entry_ids": ["decision-002a"]}],
    )
    (processed / "strokes.json").unlink(missing_ok=True)

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert results[0].orphan_ambiguities == []
    assert [p.title for p in results[0].recorded] == [clean]
    assert [p.title for p in results[0].held] == [murky]


# --- Finding 5: receipts validated, staged, journalled ---------------------


@pytest.mark.parametrize(
    "content",
    [b"", b"not json at all", b"{}", b'{"tool": "something-else"}'],
    ids=["zero-byte", "garbage", "empty-object", "wrong-tool"],
)
def test_an_unreadable_receipt_holds_instead_of_silently_skipping(workspace, content):
    processed, item_id = one_row_document(workspace)
    document = cd.Document(processed)
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    cd.receipt_path(receipts, document.page_id, item_id).write_bytes(content)
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert results[0].recorded == []
    assert results[0].already == []
    assert "receipt" in results[0].held[0].hold_text()
    assert workspace["queue"].read_bytes() == before


def test_a_valid_receipt_still_makes_the_row_a_no_op(workspace):
    processed, _ = one_row_document(workspace)
    first, first_code = run(workspace, [processed], apply=True)
    assert first_code == cd.EXIT_OK and len(first[0].recorded) == 1
    after = workspace["queue"].read_bytes()

    second, second_code = run(workspace, [processed], apply=True)

    assert second_code == cd.EXIT_OK
    assert len(second[0].already) == 1
    assert workspace["queue"].read_bytes() == after


def test_a_receipt_that_fails_staging_leaves_the_queue_untouched(
    workspace, monkeypatch
):
    processed, _ = one_row_document(workspace)
    before = workspace["queue"].read_bytes()

    def exploding_stage(*args, **kwargs):
        raise OSError("the receipts disk is full")

    monkeypatch.setattr(cd, "stage_receipts", exploding_stage)

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_ERROR
    assert workspace["queue"].read_bytes() == before
    assert list(workspace["receipts"].glob("*.json")) == [] or not workspace[
        "receipts"
    ].exists()


def test_a_crash_after_the_queue_write_is_recovered_on_the_next_run(
    workspace, monkeypatch
):
    """Queue written, receipts not yet published: the next run publishes them."""
    processed, item_id = one_row_document(workspace)

    def crash_after_commit(*args, **kwargs):
        raise KeyboardInterrupt("power cut between the queue write and publish")

    monkeypatch.setattr(cd, "publish_receipts", crash_after_commit)
    with pytest.raises(KeyboardInterrupt):
        run(workspace, [processed], apply=True)

    # The queue moved, the receipt did not land, and a journal survives.
    assert "## Pen dispositions" in workspace["queue"].read_text(encoding="utf-8")
    journals = list(workspace["receipts"].glob("journal-*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text())["state"] == "queue_written"
    assert not cd.receipt_path(
        workspace["receipts"], cd.Document(processed).page_id, item_id
    ).exists()

    monkeypatch.undo()
    messages = cd.recover_journals(workspace["receipts"], workspace["queue"])

    assert any("had landed" in message for message in messages)
    assert cd.receipt_path(
        workspace["receipts"], cd.Document(processed).page_id, item_id
    ).is_file()
    assert list(workspace["receipts"].glob("journal-*.json")) == []


def test_a_crash_before_the_queue_write_discards_the_staged_receipts(workspace):
    processed, item_id = one_row_document(workspace)
    document = cd.Document(processed)
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    queue_sha = cd.tp.sha256_bytes(workspace["queue"].read_bytes())

    plan = cd.RowPlan(item_id, "Row under review (mc-r1)?", document.decisions)
    plan.disposition = "PEN 2026-08-16: YES."
    staged = cd.stage_receipts(receipts, "crashrun", document, [plan], queue_sha)
    cd.tp.atomic_json(
        cd.journal_path(receipts, "crashrun"),
        {
            "tool": "consume_decisions",
            "schema_version": cd.JOURNAL_SCHEMA_VERSION,
            "run_id": "crashrun", "state": "prepared",
            "queue_path": str(workspace["queue"]), "queue_sha_before": queue_sha,
            "queue_sha_after": "0" * 64,
            "staging": str(receipts / "staging-crashrun"), "receipts": staged,
            # _apply_document always writes the page id; a journal that
            # declares receipts without one names no row, and is refused.
            "page_id": document.page_id,
        },
    )

    messages = cd.recover_journals(receipts, workspace["queue"])

    assert any("had NOT landed" in message for message in messages)
    assert not cd.receipt_path(receipts, document.page_id, item_id).exists()
    assert list(receipts.glob("journal-*.json")) == []
    assert not (receipts / "staging-crashrun").exists()


def test_a_journal_that_matches_neither_state_refuses_to_continue(workspace):
    processed, _ = one_row_document(workspace)
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    cd.tp.atomic_json(
        cd.journal_path(receipts, "mystery"),
        {
            "tool": "consume_decisions",
            "schema_version": cd.JOURNAL_SCHEMA_VERSION,
            "run_id": "mystery", "state": "prepared",
            "queue_path": str(workspace["queue"]),
            "queue_sha_before": "1" * 64, "queue_sha_after": "2" * 64,
            "staging": str(receipts / "staging-mystery"), "receipts": [],
        },
    )
    before = workspace["queue"].read_bytes()

    # run() raises so a caller cannot miss it; main() turns it into exit 2.
    with pytest.raises(cd.ConsumeError) as excinfo:
        run(workspace, [processed], apply=True)
    assert "cannot be reconciled" in str(excinfo.value)

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply", "--no-alert",
        ]
    )

    assert code == cd.EXIT_ERROR
    assert workspace["queue"].read_bytes() == before


# --- Finding 6: the alert must be honest ----------------------------------


def alert_capture(tmp_path, monkeypatch):
    record = tmp_path / "alert.txt"
    helper = tmp_path / "capture.sh"
    helper.write_text(
        "#!/bin/bash\nprintf '%s\\n---\\n%s\\n' \"$1\" \"$2\" > " + str(record) + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setattr(cd, "BUS_ALERT", helper)
    return record


def test_an_apply_failure_still_sends_an_alert_saying_the_queue_did_not_move(
    workspace, tmp_path, monkeypatch
):
    processed, _ = one_row_document(workspace)
    _forge(processed, lambda d: d["decisions"][0].__setitem__("choice", "MAYBE"))
    record = alert_capture(tmp_path, monkeypatch)

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply",
        ]
    )

    assert code == cd.EXIT_ERROR
    text = record.read_text(encoding="utf-8")
    assert "No queue change was made." in text
    assert "MAYBE" in text


def test_a_run_level_failure_still_sends_an_alert(workspace, tmp_path, monkeypatch):
    processed, _ = one_row_document(workspace)
    record = alert_capture(tmp_path, monkeypatch)

    def explode(*args, **kwargs):
        raise cd.ConsumeError("the lock could not be taken")

    monkeypatch.setattr(cd, "run", explode)

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply",
        ]
    )

    assert code == cd.EXIT_ERROR
    text = record.read_text(encoding="utf-8")
    assert "FAILED" in text
    assert "the lock could not be taken" in text


def test_a_successful_run_that_records_nothing_says_so_in_the_alert(
    workspace, tmp_path, monkeypatch
):
    processed, _ = one_row_document(workspace, title="A row that moved on (mc-r4)?")
    # Rewrite the queue so the answered row is no longer in Active.
    write_queue(
        workspace["queue"],
        [
            {
                "title": "A completely different row",
                "project": "other",
                "age": "2026-08-11",
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    record = alert_capture(tmp_path, monkeypatch)

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply",
        ]
    )

    assert code == cd.EXIT_HOLD
    text = record.read_text(encoding="utf-8")
    assert "No queue change was made." in text
    assert "HELD" in text


def test_two_candidates_on_one_card_hold_that_row_and_do_not_block(workspace):
    """A mark between two options of the SAME card names two entries, one row.

    The 2026-08-08 document does exactly this (page-1-line-34 and -35 name
    decision-001a and decision-001b). Counting distinct rows instead of
    distinct entries mistook it for manifest skew and blocked the document.
    """
    title = "One card, two options (mc-r5)?"
    other = "An unrelated row (mc-r6)?"
    project, age = "example-project", "2026-08-10"
    write_queue(
        workspace["queue"],
        [
            {"title": title, "project": project, "age": age,
             "ask": ask_for(["First?", "Second?"])},
            {"title": other, "project": project, "age": age,
             "ask": ask_for(["Go?"])},
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {"item_id": item_id_for(title, project, age), "headline": title,
             "options": [("First?", "YES"), ("Second?", "NO")]},
            {"item_id": item_id_for(other, project, age), "headline": other,
             "options": [("Go?", "YES")]},
        ],
        ambiguities=[{
            "stroke_id": "page-1-line-34", "reason": "mark_crosses_boxes", "page": 1,
            "candidate_entry_ids": ["decision-001a", "decision-001b"],
        }],
    )
    (processed / "strokes.json").unlink(missing_ok=True)

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert results[0].orphan_ambiguities == []
    assert [p.title for p in results[0].held] == [title]
    assert results[0].held[0].hold_reasons == ["ambiguity_touches_row"]
    assert [p.title for p in results[0].recorded] == [other]


# ---------------------------------------------------------------------------
# Notes are first-class output, never a defect (2026-08-19 finding).
#
# The reader writes on these pages. The 2026-08-16 document carries eight cards of
# his handwriting, unread for three days, including "Go ahead and deploy now"
# and "Do this today so I can review before tomorrow". An earlier version of
# the fail-closed rule blocked that whole document on two of those strokes,
# withholding twenty verified answers AND treating his prose as a fault.
# ---------------------------------------------------------------------------


def ink_document_with_note(
    base: Path, date: str, queue: Path, note_where: str = "body"
) -> tuple[Path, dict]:
    """Real manifest, a real tick, and one long stroke that is handwriting."""
    rows = tp.parse_review_queue(queue, require_keys=False)
    manifest = build_legacy_manifest(date, rows)
    entry = next(e for e in manifest["entries"] if e["kind"] == "decision")
    card = next(c for c in manifest["cards"] if c["card_id"] == entry["card_id"])
    yes_box = next(box for box in entry["boxes"] if box["choice"] == "YES")
    layout = card["layout"]

    if note_where == "body":
        # A long stroke across the printed body, clear of every answer box.
        x0 = layout["x"] + 5
        x1 = layout["x"] + layout["width"] * 0.5
        y0 = (layout["bottom"] + layout["top"]) / 2
        y1 = y0 + 20
    else:  # inside the note lines the page prints for writing
        nx0, ny0, nx1, ny1 = layout["write_in_bbox"]
        x0, y0, x1, y1 = nx0 + 2, ny0 + 2, nx1 - 2, ny1 - 2
    scrawl = {
        "stroke_id": "scrawl-1",
        "page": entry["page"],
        "points": [[x0, y0], [(x0 + x1) / 2, y1], [x1, y0]],
    }
    strokes = {
        "schema_version": manifest["schema_version"],
        "layout_version": manifest["layout_version"],
        "page_id": manifest["page_id"],
        "strokes": [tick(yes_box["bbox"], page=entry["page"]), scrawl],
    }
    resolution = tp.resolve(manifest, strokes)

    folder = base / date
    processed = folder / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (folder / f"decisions-{date}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (processed / "strokes.json").write_text(
        json.dumps(strokes, indent=2), encoding="utf-8"
    )
    (processed / "resolution.json").write_text(
        json.dumps(resolution, indent=2), encoding="utf-8"
    )
    return processed, resolution


def test_a_note_stroke_never_blocks_a_document(real_ink_queue):
    """Handwriting across a card is prose. The answer on that page still lands."""
    workspace = real_ink_queue
    processed, resolution = ink_document_with_note(
        workspace["archive"], "2026-08-16", workspace["queue"], note_where="body"
    )
    document = cd.Document(processed)
    touched, blocking, notes = document.ambiguous_items()

    assert blocking == [], "handwriting must never block a document"
    assert len(notes) == 1

    results, code = run(workspace, [processed], apply=True)

    assert results[0].orphan_ambiguities == []
    assert len(results[0].recorded) == 1, "the answer must still be recorded"
    assert results[0].held == []
    # Unread handwriting is still a reason to look, so the run is not "clean".
    assert code == cd.EXIT_HOLD
    assert tp.parse_review_queue(workspace["queue"], require_keys=False) == []


def test_a_mark_in_the_answer_boxes_still_blocks(real_ink_queue):
    """The fail-closed rule survives: an unreadable ANSWER still blocks."""
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"], stale_ambiguity=False
    )
    document = cd.Document(processed)
    entry = next(
        e for e in document.manifest["entries"] if e["kind"] == "decision"
    )
    box = entry["boxes"][0]["bbox"]
    # A mark centred right on an answer box, naming nobody.
    forged = {
        "stroke_id": "unreadable",
        "reason": "mark_too_large",
        "page": entry["page"],
        "bbox": [box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1],
    }
    assert document.classify_mark(forged) is None

    stored = json.loads((processed / "resolution.json").read_text(encoding="utf-8"))
    stored["ambiguities"] = [forged]
    (processed / "resolution.json").write_text(
        json.dumps(stored, indent=2), encoding="utf-8"
    )
    (processed / "strokes.json").unlink()
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_HOLD
    assert len(results[0].orphan_ambiguities) == 1
    assert results[0].recorded == []
    assert workspace["queue"].read_bytes() == before


def test_a_mark_with_no_geometry_still_fails_closed(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"], stale_ambiguity=False
    )
    document = cd.Document(processed)
    assert document.classify_mark({"stroke_id": "x", "reason": "mark_too_large"}) is None


def test_unread_notes_appear_in_the_report_with_their_images(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = ink_document_with_note(
        workspace["archive"], "2026-08-16", workspace["queue"], note_where="notes"
    )

    results, code = run(workspace, [processed], apply=False)
    report = cd.render_report(results, apply=False)

    assert len(results[0].notes) == 1
    note = results[0].notes[0]
    assert note.png == processed / "notes" / f"{note.card_id}.png"
    assert "NOTES TO READ" in report
    assert "A human has to read these" in report
    assert str(note.png) in report
    assert "not rendered yet" in report  # the PNG does not exist in this fixture
    assert "notes to read: 1" in report
    assert "NOT DONE" in report


def test_a_card_with_both_a_note_and_an_answer_records_and_surfaces(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = ink_document_with_note(
        workspace["archive"], "2026-08-16", workspace["queue"], note_where="notes"
    )

    results, code = run(workspace, [processed], apply=True)

    assert len(results[0].recorded) == 1
    assert len(results[0].notes) == 1
    assert results[0].notes[0].answered is True
    assert "(this card was also answered)" in results[0].notes[0].describe()
    assert "## Pen dispositions" in workspace["queue"].read_text(encoding="utf-8")


def test_the_alert_says_notes_are_unread(real_ink_queue, tmp_path, monkeypatch):
    workspace = real_ink_queue
    processed, _ = ink_document_with_note(
        workspace["archive"], "2026-08-16", workspace["queue"], note_where="notes"
    )
    record = alert_capture(tmp_path, monkeypatch)

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply",
        ]
    )

    assert code == cd.EXIT_HOLD
    text = record.read_text(encoding="utf-8")
    assert "handwritten notes UNREAD" in text
    assert "This is not a finished run." in text
    assert "NOTE 2026-08-16:" in text


# --- an unsupported document is skipped, not fatal -------------------------


def test_an_unsupported_layout_is_skipped_and_the_rest_of_the_run_continues(
    workspace,
):
    good, _ = one_row_document(workspace, title="A current document (mc-r7)?")
    old = build_document(
        workspace["archive"],
        "2026-07-28",
        [{"item_id": "aaaaaaaaaaaa", "headline": "An old card", "options": [("Go?", None)]}],
    )
    # decisions-v2 predates the allowlisted v2.1-v2.3 run and is genuinely
    # unsupported: this must still be skipped, not consumed.
    _forge(old, lambda d: d.__setitem__("layout_version", "decisions-v2"))

    results, code = run(workspace, [old, good], apply=True)

    old_result = next(r for r in results if r.document.date == "2026-07-28")
    good_result = next(r for r in results if r.document.date == "2026-08-16")
    assert old_result.skipped and "decisions-v2" in old_result.skipped
    assert old_result.error is None
    # The current document still processed, and the run is not exit 2.
    assert len(good_result.recorded) == 1
    assert code == cd.EXIT_OK
    report = cd.render_report(results, apply=True)
    assert "SKIPPED" in report
    assert "documents skipped: 1" in report


# --- mc-vjz5: LAYOUT_VERSION bump must not refuse older ink-compatible ------
# --- documents (the appendix-markdown fix that broke every archived doc). --


@pytest.mark.parametrize("layout_version", ["decisions-v2.1", "decisions-v2.2"])
def test_an_older_allowlisted_layout_version_still_validates_and_replays(
    workspace, layout_version
):
    """LAYOUT_VERSION has moved on to v2.3, but v2.1 and v2.2 documents were
    never rewritten - the whole existing archive is at an older version. A
    document at any layout version in tp.ANSWER_COMPATIBLE_LAYOUT_VERSIONS
    must still validate and be consumable, not just current-LAYOUT_VERSION
    documents.
    """
    processed, item_id = one_row_document(workspace)
    _forge(processed, lambda d: d.__setitem__("layout_version", layout_version))

    document = cd.Document(processed)
    document.validate()  # must not raise UnsupportedDocumentError

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_OK
    assert len(results[0].recorded) == 1
    assert results[0].error is None
    assert results[0].skipped is None or results[0].skipped == ""


def test_a_genuinely_unknown_layout_version_is_still_refused(workspace):
    """The allowlist is not "accept anything" - a document written by a tool
    this code does not recognise must still fail closed. This is the exact
    safety property the layout_version check exists for.
    """
    processed, _ = one_row_document(workspace)
    _forge(processed, lambda d: d.__setitem__("layout_version", "decisions-v99"))

    document = cd.Document(processed)
    with pytest.raises(cd.UnsupportedDocumentError) as excinfo:
        document.validate()
    assert "decisions-v99" in str(excinfo.value)

    results, code = run(workspace, [processed], apply=True)

    assert results[0].skipped and "decisions-v99" in results[0].skipped
    assert results[0].recorded == []
    assert code == cd.EXIT_OK


# ---------------------------------------------------------------------------
# The archive is evidence (2026-08-19 finding).
#
# The eight rendered note PNGs for 2026-08-16 are the only rendering of
# instructions the reader gave us that nobody read for three days. They are the
# record of what his device produced on the day, not scratch space. A dry run
# must not write a single byte outside its temp directory, and even an apply
# must never write over something already in an archive folder.
# ---------------------------------------------------------------------------


def hash_tree(root: Path) -> dict[str, str]:
    """Every file under root, by relative path, with its content hash."""
    return {
        str(path.relative_to(root)): tp.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_second_apply_on_the_same_day_does_not_land_on_the_first(real_ink_queue):
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"]
    )
    document = cd.Document(processed)

    first = document.write_reresolved()
    second = document.write_reresolved()

    assert first is not None and second is not None
    assert first != second
    assert first.exists() and second.exists()
    assert second.name.endswith("-2.json")


def test_this_module_contains_no_image_writing_code():
    """A structural guard: rendering belongs to today_page, not here.

    The archived PNGs were rewritten while this ticket was in flight. This
    module was not the cause and must never become one.
    """
    source = (ROOT / "remarkable" / "consume_decisions.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "render_note_region_pngs",
        "process_archive",
        "extract_v6",
        ".save(",
        "ImageDraw",
    ):
        assert forbidden not in source, (
            f"{forbidden} appeared in consume_decisions.py; this module reads "
            "archived renderings, it never writes them"
        )


# ---------------------------------------------------------------------------
# Every stroke is accounted for (2026-08-19 finding).
#
# The rendered PNGs were cropped: notes.json only ever held the strokes the
# resolver attributed to the two ruled note lines, so writing above them, on
# the printed card, or in the gap between cards was rendered nowhere. The
# 2026-08-08 card-001 image dropped a boxed four-line paragraph reading "Also:
# Why am I being asked questions I've already answered? This is a pattern and I
# dont like it", and 2026-08-16 card-018 dropped its first line. 191 strokes
# across the archive were in no image at all.
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "remarkable"))
import importlib.util as _importlib_util  # noqa: E402

_spec = _importlib_util.spec_from_file_location(
    "render_archived_notes", ROOT / "remarkable" / "render-archived-notes.py"
)
render_notes = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(render_notes)


def replayable_archives() -> list[tuple[str, dict, dict, dict]]:
    """Every archived page this build can replay, as (date, manifest, ink, resolution)."""
    found = []
    for processed in cd.discover_processed_dirs():
        try:
            document = cd.Document(processed)
            document.validate()
        except cd.ValidationError:
            continue
        if document.manifest is None or document.resolution_source != "re-resolved":
            continue
        strokes = json.loads(
            (processed / "strokes.json").read_text(encoding="utf-8")
        )
        found.append((document.date, document.manifest, strokes, document.resolution))
    return found


# ---------------------------------------------------------------------------
# Cross-review round 2 (BLOCK on 09b3ae11). Six findings, each reproduced
# against the production functions before it was fixed.
#
# The stakes set the bar: this tool exists because 41 handwritten notes and 30
# pen answers were captured and never acted on. Losing a note or closing a row
# The reader did not answer is worse than doing nothing at all.
# ---------------------------------------------------------------------------


# --- R2 finding 1: every queue writer shares one lock ----------------------


def test_a_competing_writer_at_the_replacement_seam_is_not_lost(
    workspace, monkeypatch
):
    """The window the compare-and-swap cannot see: after the hash check.

    commit_queue verified the live hash and then handed off to a separate
    replacement step. A writer landing in between was compared against nothing
    and silently overwritten. The competing write here is today_page's own
    queue writer, the one the 06:00 generator and today_page.execute use, and
    it is injected INSIDE the replacement, after any comparison.
    """
    title = "Row under review (mc-r1)?"
    other = "A row the other writer closes"
    write_queue(
        workspace["queue"],
        [
            {
                "title": title,
                "project": "example-project",
                "age": "2026-08-10",
                "ask": ask_for(["Go?"]),
            },
            {
                "title": other,
                "project": "other",
                "age": "2026-08-11",
                "ask": ask_for(["Leave this alone?"]),
            },
        ],
    )
    processed = build_document(
        workspace["archive"],
        "2026-08-16",
        [
            {
                "item_id": item_id_for(title, "example-project", "2026-08-10"),
                "headline": title,
                "options": [("Go?", "YES")],
            }
        ],
    )
    queue = workspace["queue"]
    other_id = item_id_for(other, "other", "2026-08-11")
    writer = (
        "import sys; sys.path.insert(0, %r)\n"
        "import today_page as tp\n"
        "from pathlib import Path\n"
        "tp.close_review_queue_row(Path(%r), %r)\n"
        % (str(ROOT / "remarkable"), str(queue), other_id)
    )

    real_replace = os.replace
    state: dict = {"fired": 0, "proc": None}

    def replace_with_a_writer_in_the_window(src, dst, *args, **kwargs):
        if state["fired"] == 0 and Path(dst) == queue:
            state["fired"] = 1
            state["proc"] = subprocess.Popen(
                [sys.executable, "-c", writer],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Long enough that an unsynchronised writer has certainly landed.
            time.sleep(1.0)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_with_a_writer_in_the_window)
    results, code = run(workspace, [processed], apply=True)
    monkeypatch.undo()

    assert state["fired"] == 1, "the replacement seam was never exercised"
    proc = state["proc"]
    assert proc is not None
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err.decode()

    assert code == cd.EXIT_OK
    assert len(results[0].recorded) == 1
    text = queue.read_text(encoding="utf-8")
    active = text.split("## Pen dispositions")[0]
    # Neither writer may be lost: our disposition AND their close both survive.
    assert "PEN 2026-08-16" in text
    assert other not in active, (
        "the competing writer's close was overwritten; its work vanished"
    )
    assert tp.parse_review_queue(queue, require_keys=False) == []


def test_today_page_takes_the_same_lock_as_the_consumer(tmp_path):
    """A lock only one writer takes is not a lock. Prove today_page takes it."""
    queue = tmp_path / "REVIEW-QUEUE.md"
    write_queue(
        queue,
        [
            {
                "title": "A row",
                "project": "p",
                "age": "2026-08-10",
                "ask": ask_for(["Go?"]),
            }
        ],
    )
    item = item_id_for("A row", "p", "2026-08-10")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import today_page as tp\n"
        "from pathlib import Path\n"
        "tp.close_review_queue_row(Path(%r), %r)\n"
        % (str(ROOT / "remarkable"), str(queue), item)
    )
    with cd.queue_lock(queue):
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(1.0)
        assert proc.poll() is None, (
            "today_page wrote the queue while another writer held the lock"
        )
        assert len(tp.parse_review_queue(queue, require_keys=False)) == 1
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err.decode()
    assert tp.parse_review_queue(queue, require_keys=False) == []


# --- R2 finding 2: a journal is a claim, never an instruction --------------


def a_journal(receipts_dir: Path, run_id: str, **overrides) -> Path:
    """A journal file shaped like the one _apply_document writes."""
    receipts_dir.mkdir(parents=True, exist_ok=True)
    staging = receipts_dir / f"staging-{run_id}"
    journal = {
        "tool": "consume_decisions",
        "schema_version": getattr(cd, "JOURNAL_SCHEMA_VERSION", 1),
        "run_id": run_id,
        "state": "published",
        "queue_path": str(
            overrides.pop("queue_path", receipts_dir / "REVIEW-QUEUE.md")
        ),
        "queue_sha_before": "0" * 64,
        "queue_sha_after": "1" * 64,
        "staging": str(staging),
        "receipts": [],
        "page_id": "decisions-2026-08-16-fixture",
        "prepared_at": "2026-08-19T01:00:00-07:00",
    }
    journal.update(overrides)
    path = receipts_dir / f"journal-{run_id}.json"
    path.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    return path


def test_a_hostile_journal_cannot_delete_archived_evidence(workspace):
    """The severe one: a journal pointed rmtree at the archive and it obeyed.

    A journal is a file on disk. Trusting its "staging" field with an
    unconditional rmtree lets any writable file delete the reader's notes, which are
    the whole reason this tool exists.
    """
    queue = workspace["queue"]
    write_queue(queue, [])
    evidence = workspace["archive"] / "2026-08-16" / "processed" / "notes"
    evidence.mkdir(parents=True)
    note = evidence / "card-001.png"
    note.write_bytes(b"\x89PNG the handwriting nobody has read yet")

    a_journal(
        workspace["receipts"],
        "20260819T010000-hostile",
        staging=str(evidence),
        queue_path=str(queue),
    )

    with pytest.raises(cd.ConsumeError) as excinfo:
        cd.recover_journals(workspace["receipts"], queue)

    assert "staging" in str(excinfo.value).lower() or "receipts" in str(excinfo.value)
    assert note.is_file(), "an archived note was deleted by a journal's say-so"
    assert note.read_bytes().startswith(b"\x89PNG")


def test_a_journal_from_another_run_is_refused(workspace):
    queue = workspace["queue"]
    write_queue(queue, [])
    path = a_journal(
        workspace["receipts"], "20260819T010000-aaaaaaaa", queue_path=str(queue)
    )
    # The file names one run; the content claims another.
    content = json.loads(path.read_text(encoding="utf-8"))
    content["run_id"] = "20260101T000000-bbbbbbbb"
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    staging = workspace["receipts"] / "staging-20260819T010000-aaaaaaaa"
    staging.mkdir(parents=True)

    with pytest.raises(cd.ConsumeError):
        cd.recover_journals(workspace["receipts"], queue)

    assert path.is_file(), "the journal was consumed instead of refused"
    assert staging.is_dir()


def test_a_journal_naming_a_different_queue_is_refused(workspace, tmp_path):
    queue = workspace["queue"]
    write_queue(queue, [])
    a_journal(
        workspace["receipts"],
        "20260819T010000-elsewhere",
        queue_path=str(tmp_path / "some-other-REVIEW-QUEUE.md"),
    )

    with pytest.raises(cd.ConsumeError):
        cd.recover_journals(workspace["receipts"], queue)


@pytest.mark.parametrize("escape", ["dotdot", "symlink"], ids=["dotdot", "symlink"])
def test_a_journal_path_that_escapes_the_receipts_directory_is_refused(
    workspace, escape
):
    """Containment is checked after realpath, so .. and symlinks both fail."""
    queue = workspace["queue"]
    write_queue(queue, [])
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    outside = workspace["archive"] / "2026-08-16" / "processed" / "notes"
    outside.mkdir(parents=True)
    note = outside / "card-002.png"
    note.write_bytes(b"\x89PNG more unread handwriting")

    run_id = f"20260819T010000-{escape}"
    if escape == "dotdot":
        staging = str(receipts / ".." / outside.relative_to(workspace["root"]))
    else:
        link = receipts / f"staging-{run_id}"
        link.symlink_to(outside, target_is_directory=True)
        staging = str(link)

    a_journal(receipts, run_id, staging=staging, queue_path=str(queue))

    with pytest.raises(cd.ConsumeError):
        cd.recover_journals(receipts, queue)

    assert note.is_file(), "a path outside the receipts directory was deleted"


def test_a_journal_receipt_path_outside_the_receipts_directory_is_refused(
    workspace, tmp_path
):
    """Publishing is a move: an unchecked destination overwrites anything."""
    queue = workspace["queue"]
    write_queue(queue, [])
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    run_id = "20260819T010000-publish"
    staging = receipts / f"staging-{run_id}"
    staging.mkdir(parents=True)
    staged = staging / "receipt.json"
    staged.write_text("{}", encoding="utf-8")
    victim = tmp_path / "important.json"
    victim.write_text('{"keep": "me"}', encoding="utf-8")

    a_journal(
        receipts,
        run_id,
        state="prepared",
        queue_path=str(queue),
        queue_sha_after=tp.sha256_bytes(queue.read_bytes()),
        receipts=[{"staged": str(staged), "final": str(victim)}],
    )

    with pytest.raises(cd.ConsumeError):
        cd.recover_journals(receipts, queue)

    assert victim.read_text(encoding="utf-8") == '{"keep": "me"}'


def test_a_good_journal_is_still_recovered(workspace):
    """The containment rules must not break honest recovery."""
    processed, _ = one_row_document(workspace)
    results, code = run(workspace, [processed], apply=True)
    assert code == cd.EXIT_OK
    assert list(workspace["receipts"].glob("journal-*.json")) == []
    assert len(list(workspace["receipts"].glob("*.json"))) == 1


# --- R3 finding 1: recovery must never delete the last evidence ------------


def _landed_journal(
    workspace, run_id: str, page_id: str, item_id: str, state: str = "queue_written"
) -> tuple[Path, Path, Path]:
    """A journal for a run whose queue change LANDED, declaring one receipt."""
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    staging = receipts / f"staging-{run_id}"
    staging.mkdir(parents=True, exist_ok=True)
    final = cd.receipt_path(receipts, page_id, item_id)
    journal = cd.journal_path(receipts, run_id)
    cd.tp.atomic_json(
        journal,
        {
            "tool": "consume_decisions",
            "schema_version": cd.JOURNAL_SCHEMA_VERSION,
            "run_id": run_id,
            "state": state,
            "queue_path": str(workspace["queue"]),
            "queue_sha_before": "0" * 64,
            # The live queue already looks like the state this run intended.
            "queue_sha_after": cd.tp.sha256_bytes(workspace["queue"].read_bytes()),
            "staging": str(staging),
            "receipts": [
                {"staged": str(staging / final.name), "final": str(final)}
            ],
            "page_id": page_id,
            "prepared_at": "2026-08-19T01:00:00-07:00",
        },
    )
    return journal, staging, final


def test_recovery_refuses_to_delete_the_evidence_of_a_missing_receipt(workspace):
    """The queue moved, the receipt is nowhere, and that must stay visible.

    Recovery filtered missing staged files out of the publish set, published
    nothing, marked the journal published and then deleted both the staging
    directory and the journal. The queue row stayed closed with no receipt and
    no record that anything had gone wrong - the only evidence of the
    inconsistency, deleted to tidy up (mc-63x2).
    """
    processed, item_id = one_row_document(workspace)
    page_id = cd.Document(processed).page_id
    journal, staging, final = _landed_journal(
        workspace, "20260819T010000-vanished", page_id, item_id
    )
    assert not final.exists()
    assert not (staging / final.name).exists()

    with pytest.raises(cd.ConsumeError) as excinfo:
        cd.recover_journals(workspace["receipts"], workspace["queue"])

    message = str(excinfo.value)
    assert "MISSING" in message and item_id in message
    assert journal.is_file(), "the journal, the last evidence, was deleted"
    assert staging.is_dir(), "the staging directory was deleted"
    assert not final.exists(), "a receipt was invented for a row with none"


def test_recovery_refuses_when_a_staged_receipt_is_not_a_receipt(workspace):
    """A staged file that exists but says nothing is not evidence either."""
    processed, item_id = one_row_document(workspace)
    page_id = cd.Document(processed).page_id
    journal, staging, final = _landed_journal(
        workspace, "20260819T010000-hollow", page_id, item_id
    )
    (staging / final.name).write_bytes(b"")

    with pytest.raises(cd.ConsumeError):
        cd.recover_journals(workspace["receipts"], workspace["queue"])

    assert journal.is_file() and staging.is_dir()
    assert not final.exists()


def test_recovery_accepts_a_receipt_that_was_already_published(workspace):
    """Honest recovery still finishes: the crash was AFTER the move.

    This is the case the new evidence rule must not break - the staged file is
    gone because os.replace already moved it into place.
    """
    processed, item_id = one_row_document(workspace)
    document = cd.Document(processed)
    receipts = workspace["receipts"]
    receipts.mkdir(parents=True, exist_ok=True)
    queue_sha = cd.tp.sha256_bytes(workspace["queue"].read_bytes())
    plan = cd.RowPlan(item_id, "Row under review (mc-r1)?", document.decisions)
    plan.disposition = "PEN 2026-08-16: YES."
    cd.stage_receipts(receipts, "20260819T010000-late", document, [plan], queue_sha)
    journal, staging, final = _landed_journal(
        workspace, "20260819T010000-late", document.page_id, item_id
    )
    # The move already happened; only the final receipt is left.
    os.replace(staging / final.name, final)
    published_bytes = final.read_bytes()

    messages = cd.recover_journals(receipts, workspace["queue"])

    assert any("had landed" in message for message in messages)
    assert final.read_bytes() == published_bytes
    assert not journal.exists() and not staging.exists()


# --- R3 finding 2: a journal cannot forge a receipt ------------------------


@pytest.mark.parametrize(
    "forgery",
    ["different-answer", "not-a-receipt"],
    ids=["different-answer", "not-a-receipt"],
)
def test_a_contained_staged_file_cannot_overwrite_a_valid_receipt(
    workspace, forgery
):
    """Shape-valid and contained is not authentic.

    validate_journal proved every path sat inside the receipts directory but
    never required the staged file to BE the receipt it claimed to publish,
    and publish_receipts replaced the destination unconditionally. A contained
    file called other-name.json therefore overwrote a real receipt - the
    record of what the reader actually answered - with whatever it contained.
    """
    processed, item_id = one_row_document(workspace)
    results, code = run(workspace, [processed], apply=True)
    assert code == cd.EXIT_OK and len(results[0].recorded) == 1
    document = cd.Document(processed)
    receipts = workspace["receipts"]
    final = cd.receipt_path(receipts, document.page_id, item_id)
    genuine = final.read_bytes()
    assert json.loads(genuine)["answers"][0]["choice"] == "YES"

    run_id = f"20260819T010000-forge{forgery[:4]}"
    staging = receipts / f"staging-{run_id}"
    staging.mkdir(parents=True)
    forged = staging / "other-name.json"
    if forgery == "not-a-receipt":
        forged.write_text('{"FORGED": true}', encoding="utf-8")
    else:
        content = json.loads(genuine)
        content["answers"][0]["choice"] = "NO"
        content["disposition"] = "PEN 2026-08-16: NO."
        forged.write_text(json.dumps(content, indent=2), encoding="utf-8")

    cd.tp.atomic_json(
        cd.journal_path(receipts, run_id),
        {
            "tool": "consume_decisions",
            "schema_version": cd.JOURNAL_SCHEMA_VERSION,
            "run_id": run_id,
            "state": "queue_written",
            "queue_path": str(workspace["queue"]),
            "queue_sha_before": "0" * 64,
            "queue_sha_after": cd.tp.sha256_bytes(workspace["queue"].read_bytes()),
            "staging": str(staging),
            "receipts": [{"staged": str(forged), "final": str(final)}],
            "page_id": document.page_id,
            "prepared_at": "2026-08-19T01:00:00-07:00",
        },
    )

    with pytest.raises(cd.ConsumeError):
        cd.recover_journals(receipts, workspace["queue"])

    assert final.read_bytes() == genuine, "a receipt was forged by a journal"


def test_publish_refuses_to_replace_a_valid_receipt_with_a_different_one(
    workspace,
):
    """The last line before os.replace: even in-process, never overwrite."""
    processed, item_id = one_row_document(workspace)
    results, code = run(workspace, [processed], apply=True)
    assert code == cd.EXIT_OK
    document = cd.Document(processed)
    receipts = workspace["receipts"]
    final = cd.receipt_path(receipts, document.page_id, item_id)
    genuine = final.read_bytes()

    staging = receipts / "staging-direct"
    staging.mkdir(parents=True)
    source = staging / final.name
    content = json.loads(genuine)
    content["answers"][0]["choice"] = "NO"
    source.write_text(json.dumps(content, indent=2), encoding="utf-8")

    with pytest.raises(cd.ConsumeError) as excinfo:
        cd.publish_receipts(
            [{"staged": str(source), "final": str(final)}], receipts
        )

    assert "records something different" in str(excinfo.value)
    assert final.read_bytes() == genuine
    assert source.is_file(), "the refused file was consumed anyway"


# --- R2 finding 3: the note/answer split must fail closed ------------------


def test_a_mark_touching_an_answer_box_blocks_however_it_is_centred(real_ink_queue):
    """A big mark can cover a box while its centre sits in the prose.

    classify_mark tested the centre only, so a stroke from the card body into
    the answer column read as handwriting and the document recorded. An
    unreadable answer must fail closed wherever its centre happens to land.
    """
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"], stale_ambiguity=False
    )
    document = cd.Document(processed)
    entry = next(e for e in document.manifest["entries"] if e["kind"] == "decision")
    card = next(
        c for c in document.manifest["cards"] if c["card_id"] == entry["card_id"]
    )
    box = entry["boxes"][0]["bbox"]
    forged = {
        "stroke_id": "sweep",
        "reason": "mark_too_large",
        "page": entry["page"],
        # From well inside the printed body across into the YES box.
        "bbox": [card["layout"]["x"] + 5, box[1] - 2, box[2] + 2, box[3] + 2],
    }
    centre = ((forged["bbox"][0] + forged["bbox"][2]) / 2,
              (forged["bbox"][1] + forged["bbox"][3]) / 2)
    assert document._card_at(entry["page"], *centre) is not None, (
        "the fixture must have its centre in the card body, or it proves nothing"
    )

    assert document.classify_mark(forged) is None

    stored = json.loads((processed / "resolution.json").read_text(encoding="utf-8"))
    stored["ambiguities"] = [forged]
    (processed / "resolution.json").write_text(
        json.dumps(stored, indent=2), encoding="utf-8"
    )
    (processed / "strokes.json").unlink()
    before = workspace["queue"].read_bytes()

    results, code = run(workspace, [processed], apply=True)

    assert len(results[0].orphan_ambiguities) == 1
    assert results[0].recorded == []
    assert workspace["queue"].read_bytes() == before


def test_a_mark_in_the_corridor_between_answer_boxes_blocks(real_ink_queue):
    """Between YES and NO is not prose: it is an answer nobody can read."""
    workspace = real_ink_queue
    processed, _ = build_real_ink_document(
        workspace["archive"], "2026-08-16", workspace["queue"], stale_ambiguity=False
    )
    document = cd.Document(processed)
    entry = next(e for e in document.manifest["entries"] if e["kind"] == "decision")
    boxes = sorted((b["bbox"] for b in entry["boxes"]), key=lambda b: b[0])
    gap_left, gap_right = boxes[0][2], boxes[-1][0]
    assert gap_right > gap_left, "the fixture needs two separated boxes"
    forged = {
        "stroke_id": "between",
        "reason": "mark_too_large",
        "page": entry["page"],
        "bbox": [gap_left + 2, boxes[0][1], gap_right - 2, boxes[0][3]],
    }

    assert document.classify_mark(forged) is None


def test_handwriting_clear_of_every_box_is_still_a_note(real_ink_queue):
    """The fix must not re-break the thing it was written to protect."""
    workspace = real_ink_queue
    processed, _ = ink_document_with_note(
        workspace["archive"], "2026-08-16", workspace["queue"], note_where="body"
    )
    document = cd.Document(processed)
    touched, blocking, notes = document.ambiguous_items()

    assert blocking == []
    assert len(notes) == 1


# --- R2 finding 4: a failed apply never reads as a success -----------------


def test_a_staging_failure_never_sends_a_success_worded_alert(
    workspace, tmp_path, monkeypatch
):
    """result.error with failure=None produced "Pen answers consumed: 1".

    The counts in an alert must mean committed, never planned: a row that was
    staged and then died was not recorded anywhere.
    """
    processed, _ = one_row_document(workspace)
    record = alert_capture(tmp_path, monkeypatch)

    def refuse(*args, **kwargs):
        raise cd.ConsumeError("the receipt could not be staged")

    monkeypatch.setattr(cd, "stage_receipts", refuse)
    before = workspace["queue"].read_bytes()

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply",
        ]
    )

    assert code == cd.EXIT_ERROR
    assert workspace["queue"].read_bytes() == before
    text = record.read_text(encoding="utf-8")
    subject, body = text.split("\n---\n", 1)
    assert "FAILED" in subject, subject
    assert "Pen answers recorded" not in subject
    assert "Pen answers consumed" not in body, body
    assert "the receipt could not be staged" in body
    assert "No queue change was made." in body


def test_a_committed_run_reports_committed_counts(workspace, tmp_path, monkeypatch):
    """The honest counterpart: a real success still says what landed."""
    processed, _ = one_row_document(workspace)
    record = alert_capture(tmp_path, monkeypatch)

    code = cd.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(workspace["queue"]),
            "--receipts-dir", str(workspace["receipts"]),
            "--apply",
        ]
    )

    assert code == cd.EXIT_OK
    text = record.read_text(encoding="utf-8")
    assert "FAILED" not in text
    assert "Pen answers consumed: 1 queue rows recorded" in text


# --- R2 finding 5: a pipe in the Ask must not forge a fourth cell ----------


def test_an_ask_with_a_pipe_leaves_the_closed_row_with_exactly_three_cells(
    workspace,
):
    """The Closed table has three columns. A raw pipe silently made four.

    tp._split_row is the only row-detection authority in this repo, so a row it
    reads as four cells is a corrupt row wherever it is rendered.
    """
    ask = (
        "Background | with one pipe. It needs a choice. "
        "Recommendation: do it. Decide: (1:go) Go?"
    )
    processed, _ = one_row_document(workspace, ask=ask)

    results, code = run(workspace, [processed], apply=True)

    assert code == cd.EXIT_OK
    text = workspace["queue"].read_text(encoding="utf-8")
    closed = text.split("## Closed", 1)[1]
    rows = [
        cells
        for cells in (tp._split_row(line) for line in closed.splitlines())
        if cells is not None
    ]
    assert rows, "the Closed table lost its rows"
    for cells in rows:
        assert len(cells) == 3, f"a Closed row parsed as {len(cells)} cells: {cells}"
    # The ask is still legible to a human and still findable in the file.
    assert "with one pipe" in closed
    assert ask in text, "the original ask was lost entirely"


def test_a_pipe_in_the_ask_survives_the_today_page_writer_too(tmp_path):
    """today_page.close_review_queue_row writes the same cell the same way."""
    queue = tmp_path / "REVIEW-QUEUE.md"
    ask = "Background | pipe. It needs a choice. Recommendation: go. Decide: (1:go) Go?"
    write_queue(
        queue,
        [{"title": "A row", "project": "p", "age": "2026-08-10", "ask": ask}],
    )
    tp.close_review_queue_row(queue, item_id_for("A row", "p", "2026-08-10"))

    closed = queue.read_text(encoding="utf-8").split("## Closed", 1)[1]
    for line in closed.splitlines():
        cells = tp._split_row(line)
        if cells is not None:
            assert len(cells) == 3, f"a Closed row parsed as {len(cells)} cells"
