from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "remarkable"))

import decision_resolver_v3 as resolver  # noqa: E402
import today_page as tp  # noqa: E402



def entry(entry_id, key, answer_type, y, choices):
    boxes = []
    for index, choice in enumerate(choices):
        x0 = 400 + index * 36
        boxes.append({"choice": choice, "bbox": [x0, y, x0 + 24, y + 24]})
    write_in = None
    if answer_type in {"choice", "write_in"}:
        write_in = {"bbox": [80, y - 32, 360, y - 8]}
    return {
        "entry_id": entry_id,
        "kind": "decision",
        "item_id": "item-one",
        "card_id": "card-one",
        "question_key": key,
        "decision_number": int(entry_id.rsplit("-", 1)[1]),
        "label": f"Question {key}?",
        "answer_type": answer_type,
        "page": 1,
        "boxes": boxes,
        "write_in": write_in,
    }


def action_entry(y=720):
    return {
        "entry_id": "action-01",
        "kind": "action",
        "label": "Mark summary read",
        "page": 1,
        "boxes": [{"choice": "RUN", "bbox": [400, y, 424, y + 24]}],
        "action": {"type": "ack_item", "payload": {"item_id": "summary-1"}},
    }


def manifest(entries, *, page_id="decisions-2026-09-02-sample"):
    payload = {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": page_id,
        "date": "2026-09-02",
        "page_count": 1,
        "answerable_page_count": 1,
        "answerable_pages": [1],
        "entries": entries,
        "cards": [
            {
                "card_id": "card-one",
                "item_id": "item-one",
                "headline": "A card the reader can answer",
                "page": 1,
                "decision_entry_ids": [item["entry_id"] for item in entries],
                "question_set_hash": "fixture",
            }
        ],
        "appendices": [],
    }
    payload["manifest_hash"] = tp.manifest_digest(payload)
    return payload


def stroke(stroke_id, x, y, *, width=8, height=8, page=1):
    return {
        "stroke_id": stroke_id,
        "page": page,
        "points": [[x, y], [x + width, y + height]],
    }


def strokes_for(manifest_payload, strokes):
    return {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": manifest_payload["page_id"],
        "strokes": strokes,
    }


def decision_map(resolution):
    return {item["entry_id"]: item for item in resolution["decisions"]}


def test_synthetic_round_trip_resolves_partial_defer_discard_choice_and_write_in():
    entries = [
        entry("question-1", "partial", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"]),
        entry("question-2", "later", "yes_no", 550, ["YES", "NO", "DEFER", "DISCARD"]),
        entry("question-3", "never", "yes_no", 450, ["YES", "NO", "DEFER", "DISCARD"]),
        entry("question-4", "pick", "choice", 350, ["A", "B", "C", "DEFER", "DISCARD"]),
        entry("question-5", "words", "write_in", 250, ["DEFER", "DISCARD"]),
        entry("question-6", "blank", "yes_no", 150, ["YES", "NO", "DEFER", "DISCARD"]),
    ]
    payload = manifest(entries)
    marks = [
        stroke("yes", 402, 652),
        stroke("defer", 474, 552),
        stroke("discard", 510, 452),
        stroke("choice-c", 474, 352),
        stroke("choice-note", 120, 326, width=70),
        stroke("write-in", 120, 226, width=90),
    ]

    resolution = resolver.resolve_v3(payload, strokes_for(payload, marks))
    decisions = decision_map(resolution)

    assert decisions["question-1"]["status"] == "answered"
    assert decisions["question-1"]["choice"] == "YES"
    assert decisions["question-2"]["status"] == "deferred"
    assert decisions["question-3"]["status"] == "discarded"
    assert decisions["question-4"]["status"] == "answered"
    assert decisions["question-4"]["choice"] == "C"
    assert decisions["question-4"]["write_in"]["stroke_ids"] == ["choice-note"]
    assert decisions["question-5"]["status"] == "answered"
    assert decisions["question-5"]["choice"] is None
    assert decisions["question-6"]["status"] == "unanswered"


def test_action_and_typed_decision_resolve_on_the_same_document():
    entries = [
        action_entry(),
        entry("question-1", "approve", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"]),
    ]
    payload = manifest(entries)
    marks = [stroke("run", 402, 722), stroke("yes", 402, 652)]

    resolution = resolver.resolve_v3(payload, strokes_for(payload, marks))

    assert resolution["actions"] == [
        {
            "entry_id": "action-01",
            "label": "Mark summary read",
            "page": 1,
            "choice": "RUN",
            "stroke_ids": ["run"],
            "action": {"type": "ack_item", "payload": {"item_id": "summary-1"}},
        }
    ]
    assert resolution["decisions"][0]["status"] == "answered"


def test_defer_combined_with_write_in_needs_review():
    entries = [
        entry("question-1", "pick", "choice", 350, ["A", "B", "DEFER", "DISCARD"])
    ]
    payload = manifest(entries)
    marks = [
        stroke("defer", 474, 352),
        stroke("words", 120, 326, width=70),
    ]

    result = resolver.resolve_v3(payload, strokes_for(payload, marks))
    decision = result["decisions"][0]

    assert decision["status"] == "needs_review"
    assert decision["marked_choices"] == ["DEFER"]
    assert decision["write_in"]["stroke_ids"] == ["words"]


def test_write_in_png_contains_only_the_named_entry_strokes(tmp_path):
    entries = [entry("question-1", "words", "write_in", 250, ["DEFER", "DISCARD"])]
    payload = manifest(entries)
    marks = [stroke("words", 120, 226, width=90)]
    ink = strokes_for(payload, marks)
    resolution = resolver.resolve_v3(payload, ink)

    rendered = resolver.render_write_in_pngs(payload, ink, resolution, tmp_path)

    assert len(rendered) == 1
    assert rendered[0]["entry_id"] == "question-1"
    assert rendered[0]["stroke_ids"] == ["words"]
    assert Path(rendered[0]["path"]).is_file()


# --- mc-z5hy3: handwriting is never silently dropped ------------------------




def test_a_page_with_answers_only_produces_no_note_regions():
    entries = [
        entry("question-1", "approve", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"])
    ]
    payload = manifest(entries)

    resolution = resolver.resolve_v3(
        payload, strokes_for(payload, [stroke("yes", 402, 652)])
    )

    assert resolution["decisions"][0]["choice"] == "YES"
    assert resolution["note_regions"] == []


def test_ink_outside_the_answer_geometry_becomes_one_note_region():
    entries = [
        entry("question-1", "approve", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"])
    ]
    payload = manifest(entries)
    marks = [
        stroke("yes", 402, 652),
        stroke("note-word-1", 80, 300, width=40),
        stroke("note-word-2", 130, 302, width=40),
        stroke("note-line-2", 80, 275, width=90),
    ]

    resolution = resolver.resolve_v3(payload, strokes_for(payload, marks))

    assert resolution["decisions"][0]["choice"] == "YES"
    assert len(resolution["note_regions"]) == 1
    region = resolution["note_regions"][0]
    assert region["page"] == 1
    assert region["stroke_ids"] == ["note-line-2", "note-word-1", "note-word-2"]
    assert region["path"] == "notes/page-001-note-1.png"
    assert region["bbox"][0] <= 80 and region["bbox"][2] >= 170


def test_two_separated_notes_on_one_page_stay_separate_regions():
    entries = [
        entry("question-1", "approve", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"])
    ]
    payload = manifest(entries)
    marks = [
        stroke("top-note", 80, 500, width=60),
        stroke("bottom-note", 80, 120, width=60),
    ]

    resolution = resolver.resolve_v3(payload, strokes_for(payload, marks))

    assert [region["stroke_ids"] for region in resolution["note_regions"]] == [
        ["top-note"],
        ["bottom-note"],
    ]
    assert [region["path"] for region in resolution["note_regions"]] == [
        "notes/page-001-note-1.png",
        "notes/page-001-note-2.png",
    ]


def test_oversized_mark_on_a_page_with_no_controls_is_a_note_not_an_ambiguity():
    """The 2026-09-03 cover note: an underline on a page carrying no card."""
    entries = [
        entry("question-1", "approve", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"])
    ]
    payload = manifest(entries)
    payload["page_count"] = 2
    payload["manifest_hash"] = tp.manifest_digest(payload)
    marks = [stroke("underline", 60, 400, width=120, height=6, page=2)]

    resolution = resolver.resolve_v3(payload, strokes_for(payload, marks))

    assert resolution["ambiguities"] == []
    assert resolution["status"] == "resolved"
    assert [region["stroke_ids"] for region in resolution["note_regions"]] == [
        ["underline"]
    ]
    assert resolution["note_regions"][0]["path"] == "notes/page-002-note-1.png"


def test_oversized_mark_on_an_answerable_page_stays_quarantined():
    """Where there are boxes to ruin, a giant mark still fails closed."""
    entries = [
        entry("question-1", "approve", "yes_no", 650, ["YES", "NO", "DEFER", "DISCARD"])
    ]
    payload = manifest(entries)
    marks = [stroke("scrawl", 395, 645, width=120, height=30)]

    resolution = resolver.resolve_v3(payload, strokes_for(payload, marks))

    assert [item["reason"] for item in resolution["ambiguities"]] == ["mark_too_large"]
    assert resolution["status"] == "needs_review"
    assert resolution["note_regions"] == []

