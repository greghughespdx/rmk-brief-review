from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "remarkable"))

import decision_consumer_v3 as consumer  # noqa: E402
import consume_decisions as cli  # noqa: E402
import question_state as qs  # noqa: E402
import today_watch  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_live_notifier(monkeypatch, tmp_path):
    """No test or mutation may reach the phone bridge."""
    disabled = tmp_path / "external-notifier-is-disabled"
    monkeypatch.setattr(consumer, "IMESSAGE_ALERT", disabled)
    return disabled


def row_key_for(title):
    return "row_" + hashlib.sha256(title.encode()).hexdigest()[:8]


def item_id_for(title):
    return hashlib.sha256(row_key_for(title).encode("ascii")).hexdigest()[:12]


def write_queue(path: Path, title: str, keys: list[str]):
    questions = " ".join(
        f"({index}:{key}) [yes/no] Question {key}?"
        for index, key in enumerate(keys, start=1)
    )
    path.write_text(
        "\n".join(
            [
                "# Review Queue",
                "",
                "## Active - Blocking",
                "",
                "| Item | Project | Age | Ask |",
                "|---|---|---|---|",
                f"| {title} | example-project | 2026-09-01 | Context. Recommendation: proceed. Decide: {questions} Row: {row_key_for(title)} Read: docs/designs/example.md |",
                "",
                "## Closed",
                "",
                "| Item | Closed | Disposition |",
                "|---|---|---|",
                "| Older item | 2026-08-01 | Complete. |",
                "",
            ]
        ),
        encoding="utf-8",
    )


def artifacts(title: str, statuses: dict[str, tuple[str, str | None]]):
    item_id = item_id_for(title)
    card_id = "card-one"
    entries = []
    decisions = []
    for number, (key, (status, choice)) in enumerate(statuses.items(), start=1):
        entry_id = consumer.question_entry_id(item_id, key)
        marked = [choice] if choice else []
        if status == "needs_review":
            marked = ["YES", "NO"]
        entry = {
            "entry_id": entry_id,
            "item_id": item_id,
            "card_id": card_id,
            "question_key": key,
            "decision_number": number,
            "label": f"Question {key}?",
            "answer_type": "yes_no",
            "page": 1,
            "boxes": [
                {"choice": box_choice, "bbox": [0, 0, 24, 24]}
                for box_choice in ("YES", "NO", "DEFER", "DISCARD")
            ],
            "write_in": None,
        }
        entries.append(entry)
        decisions.append(
            {
                **{field: entry[field] for field in (
                    "entry_id", "item_id", "card_id", "question_key",
                    "decision_number", "label", "answer_type", "page"
                )},
                "status": status,
                "choice": choice,
                "marked_choices": marked,
                "stroke_ids": [f"stroke-{number}"] if marked else [],
                "write_in": None,
            }
        )
    manifest = {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": "decisions-2026-09-02-sample",
        "entries": entries,
        "cards": [
            {
                "card_id": card_id,
                "item_id": item_id,
                "headline": title,
                "question_set_hash": consumer.question_set_hash(statuses),
            }
        ],
    }
    resolution = {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": manifest["page_id"],
        "decisions": decisions,
    }
    return manifest, resolution


def test_partial_answer_updates_each_state_without_closing_the_row(tmp_path):
    title = "A three-part decision"
    statuses = {
        "first": ("answered", "YES"),
        "later": ("deferred", "DEFER"),
        "never": ("discarded", "DISCARD"),
    }
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda _message, _crop: None,
        receipts_dir=receipts,
    )

    assert result.closed_item_ids == []
    assert result.kept_item_ids == [item_id_for(title)]
    assert title in queue.read_text(encoding="utf-8").split("## Closed", 1)[0]
    # mc-9sy2v: one entry answered, one deferred, one discarded on the same
    # row (item_id) writes exactly one receipt, covering only the answer.
    assert sorted(p.name for p in receipts.glob("*.json")) == [
        f"{manifest['page_id']}.{item_id_for(title)}.json"
    ]
    receipt = json.loads(
        (receipts / f"{manifest['page_id']}.{item_id_for(title)}.json").read_text()
    )
    assert [a["choice"] for a in receipt["answers"]] == ["YES"]
    saved = qs.load_state(state)["entries"]
    assert [saved[consumer.question_entry_id(item_id_for(title), key)]["state"] for key in statuses] == [
        "answered", "deferred", "discarded"
    ]


def test_later_document_may_omit_terminal_questions_and_close_the_full_row(tmp_path):
    title = "A decision completed across two days"
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, ["first", "later"])
    first_manifest, first_resolution = artifacts(
        title, {"first": ("answered", "YES"), "later": ("deferred", "DEFER")}
    )
    consumer.consume(
        first_manifest,
        first_resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda _message, _crop: None,
        receipts_dir=receipts,
    )
    later_manifest, later_resolution = artifacts(
        title, {"later": ("answered", "NO")}
    )
    later_manifest["page_id"] = "decisions-2026-09-03-sample"
    later_resolution["page_id"] = later_manifest["page_id"]
    later_manifest["cards"][0]["question_set_hash"] = consumer.question_set_hash(
        ["first", "later"]
    )

    result = consumer.consume(
        later_manifest,
        later_resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-03",
        apply=True,
        notifier=lambda _message, _crop: None,
        receipts_dir=receipts,
    )

    assert result.closed_item_ids == [item_id_for(title)]
    assert title in queue.read_text(encoding="utf-8").split("## Closed", 1)[1]
    # Each page answered a different question on the same row, so each page
    # gets its OWN receipt (mc-9sy2v: a receipt is keyed by page_id AND
    # item_id, never item_id alone).
    assert sorted(p.name for p in receipts.glob("*.json")) == [
        f"{first_manifest['page_id']}.{item_id_for(title)}.json",
        f"{later_manifest['page_id']}.{item_id_for(title)}.json",
    ]


def test_row_closes_only_when_every_question_is_terminal(tmp_path):
    title = "A completed decision"
    statuses = {
        "first": ("answered", "NO"),
        "never": ("discarded", "DISCARD"),
    }
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda _message, _crop: None,
        receipts_dir=receipts,
    )

    assert result.closed_item_ids == [item_id_for(title)]
    active, closed = queue.read_text(encoding="utf-8").split("## Closed", 1)
    assert title not in active
    assert title in closed
    assert (receipts / f"{manifest['page_id']}.{item_id_for(title)}.json").is_file()


def test_stale_manifest_question_set_cannot_close_a_row(tmp_path):
    title = "A stale decision"
    statuses = {"first": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, ["first", "missing_from_manifest"])
    manifest, resolution = artifacts(title, statuses)
    manifest["cards"][0]["question_set_hash"] = consumer.question_set_hash(
        ["first", "missing_from_manifest"]
    )
    before = queue.read_bytes()

    with pytest.raises(
        consumer.DecisionConsumerV3Error,
        match="omits nonterminal or invents",
    ):
        consumer.consume(
            manifest,
            resolution,
            queue_path=queue,
            state_path=state,
            document_date="2026-09-02",
            apply=True,
            notifier=lambda _message, _crop: None,
        )

    assert queue.read_bytes() == before
    assert not state.exists()


def test_question_like_context_and_escaped_pipe_do_not_change_the_key_set(tmp_path):
    title = "A decision with table punctuation"
    statuses = {"real_key": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    text = queue.read_text(encoding="utf-8")
    text = text.replace(
        "Context. Recommendation:",
        "Context (9:not_a_question) with an escaped &#124; pipe. Recommendation:",
    )
    queue.write_text(text, encoding="utf-8")
    manifest, resolution = artifacts(title, statuses)

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda _message, _crop: None,
        receipts_dir=tmp_path / "receipts",
    )

    assert result.closed_item_ids == [item_id_for(title)]


def test_title_edit_does_not_change_the_row_or_question_identity(tmp_path):
    original_title = "The original card title"
    edited_title = "A clearer title for the same card"
    statuses = {"go_ahead": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, original_title, list(statuses))
    queue.write_text(
        queue.read_text(encoding="utf-8").replace(original_title, edited_title),
        encoding="utf-8",
    )
    manifest, resolution = artifacts(original_title, statuses)
    receipts = tmp_path / "receipts"

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda _message, _crop: None,
        receipts_dir=receipts,
    )

    assert result.closed_item_ids == [item_id_for(original_title)]
    assert edited_title in queue.read_text(encoding="utf-8").split("## Closed", 1)[1]
    receipt = json.loads(
        (receipts / f"{manifest['page_id']}.{item_id_for(original_title)}.json").read_text()
    )
    assert receipt["row_title"] == edited_title


def test_claimed_answer_must_match_the_resolution_evidence(tmp_path):
    title = "A forged decision"
    statuses = {"first": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    resolution["decisions"][0]["marked_choices"] = ["DEFER"]

    with pytest.raises(consumer.DecisionConsumerV3Error, match="status does not match"):
        consumer.consume(
            manifest,
            resolution,
            queue_path=queue,
            state_path=state,
            document_date="2026-09-02",
            apply=True,
            notifier=lambda _message, _crop: None,
        )

    assert not state.exists()


def test_manifest_controls_must_match_the_declared_answer_type(tmp_path):
    title = "A malformed answer form"
    statuses = {"first": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    manifest["entries"][0]["boxes"] = [
        box for box in manifest["entries"][0]["boxes"] if box["choice"] != "NO"
    ]

    with pytest.raises(consumer.DecisionConsumerV3Error, match="answer type"):
        consumer.consume(
            manifest,
            resolution,
            queue_path=queue,
            state_path=state,
            document_date="2026-09-02",
            apply=True,
            notifier=lambda _message, _crop: None,
        )

    assert not state.exists()


def test_dry_run_writes_nothing_and_sends_no_message(tmp_path):
    title = "An unclear decision"
    statuses = {"first": ("needs_review", None)}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    before = queue.read_bytes()
    calls = []

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=False,
        notifier=lambda message, crop: calls.append((message, crop)),
    )

    assert result.clarification_entry_ids
    assert calls == []
    assert queue.read_bytes() == before
    assert not state.exists()
    assert not state.with_name(state.name + ".lock").exists()


def test_clarification_message_is_sent_once_without_a_local_path(tmp_path):
    title = "An unclear decision"
    statuses = {"first": ("needs_review", None)}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    decision = resolution["decisions"][0]
    entry = manifest["entries"][0]
    entry["answer_type"] = "choice"
    entry["options"] = [
        {"letter": "A", "text": "first option"},
        {"letter": "B", "text": "second option"},
    ]
    entry["boxes"] = [
        {"choice": box_choice, "bbox": [0, 0, 24, 24]}
        for box_choice in ("A", "B", "DEFER", "DISCARD")
    ]
    entry["write_in"] = {"bbox": [0, 30, 100, 54]}
    decision["answer_type"] = "choice"
    decision["marked_choices"] = ["A", "DEFER"]
    decision["write_in"] = {
        "entry_id": decision["entry_id"],
        "path": "write-ins/private-crop.png",
        "stroke_ids": ["stroke-1"],
    }
    calls = []

    first = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda message, crop: calls.append((message, crop)),
    )
    second = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda message, crop: calls.append((message, crop)),
    )

    assert first.notifications_sent == [decision["entry_id"]]
    assert second.notifications_sent == []
    assert len(calls) == 1
    assert title in calls[0][0]
    assert "Question first?" in calls[0][0]
    assert "A and Defer" in calls[0][0]
    assert "write-ins" not in calls[0][0]
    assert calls[0][1] == Path("write-ins/private-crop.png")


def test_disabled_notifications_do_not_claim_that_a_message_was_sent(tmp_path):
    title = "An unclear decision"
    statuses = {"first": ("needs_review", None)}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    calls = []

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-02",
        apply=True,
        notifier=lambda message, crop: calls.append((message, crop)),
        notifications_enabled=False,
    )

    assert result.notifications_sent == []
    assert calls == []
    record = qs.load_state(state)["entries"][resolution["decisions"][0]["entry_id"]]
    assert "notified_at" not in record


def test_test_process_has_no_executable_production_notifier(isolate_live_notifier):
    assert consumer.IMESSAGE_ALERT == isolate_live_notifier
    assert not consumer.IMESSAGE_ALERT.exists()


def test_cli_dry_run_reads_schema_two_without_writing(tmp_path, capsys):
    title = "A command-line decision"
    statuses = {"first": ("answered", "YES"), "later": ("unanswered", None)}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    before = queue.read_bytes()
    manifest, resolution = artifacts(title, statuses)
    day = tmp_path / "2026-09-02"
    processed = day / "processed"
    processed.mkdir(parents=True)
    (day / "decisions-2026-09-02.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (processed / "resolution.json").write_text(
        json.dumps(resolution), encoding="utf-8"
    )

    code = cli.main(
        [
            "--processed-dir", str(processed),
            "--review-queue", str(queue),
            "--question-state", str(state),
            "--no-alert",
        ]
    )

    assert code == cli.EXIT_OK
    assert "DRY RUN" in capsys.readouterr().out
    assert queue.read_bytes() == before
    assert not state.exists()


def test_cli_malformed_schema_two_resolution_fails_without_a_traceback(
    tmp_path, capsys
):
    queue = tmp_path / "REVIEW-QUEUE.md"
    write_queue(queue, "A broken document", ["first"])
    processed = tmp_path / "2026-09-02" / "processed"
    processed.mkdir(parents=True)
    (processed / "resolution.json").write_text("{not json", encoding="utf-8")

    code = cli.main(
        ["--processed-dir", str(processed), "--review-queue", str(queue)]
    )

    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert "cannot read a resolution" in captured.err
    assert "Traceback" not in captured.err


def test_active_rows_accept_a_period_after_the_row_key(tmp_path):
    """The live queue writes 'Row: key. Read: path' and the renderer accepts it;
    the consumer must read the same key (first live v3 round trip, 2026-09-02)."""
    queue = tmp_path / "queue.md"
    queue.write_text(
        "\n".join(
            [
                "# Queue",
                "",
                "## Active - Triage",
                "",
                "| Item | Project | Age | Ask |",
                "|---|---|---|---|",
                "| Should the thing happen? | example-project | 2026-09-02 | Context. Recommendation: proceed. Decide: (1:go) [yes/no] Go? Row: thing_go. Read: docs/designs/example.md |",
                "| Should the other thing happen? | example-project | 2026-09-02 | Context. Recommendation: proceed. Decide: (1:go) [yes/no] Go? Row: other_go Read: docs/designs/example.md |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    rows = consumer.active_rows(queue)
    keys = sorted(row.row_key for row in rows.values())
    assert keys == ["other_go", "thing_go"]


def test_needs_review_accepts_the_resolver_naming_the_marked_box():
    """DEFER plus handwriting is needs_review; the resolver reports choice DEFER,
    the consumer expected None. Both describe the same ink (2026-09-02 live)."""
    entry = {
        "entry_id": "e1",
        "answer_type": "choice",
        "options": [{"letter": "A"}, {"letter": "B"}],
        "boxes": [{"choice": c} for c in ("A", "B", "DEFER", "DISCARD")],
        "write_in": {"bbox": [0, 0, 1, 1]},
    }
    base = {
        "marked_choices": ["DEFER"],
        "status": "needs_review",
        "write_in": {"entry_id": "e1", "stroke_ids": ["s1"]},
    }
    consumer._validate_decision_evidence(entry, {**base, "choice": "DEFER"})
    consumer._validate_decision_evidence(entry, {**base, "choice": None})
    with pytest.raises(consumer.DecisionConsumerV3Error):
        consumer._validate_decision_evidence(entry, {**base, "choice": "A"})


def test_note_regions_are_reported_as_unread_handwriting(tmp_path):
    """mc-z5hy3: the v3 path had no note surface, so notes reached nobody."""
    title = "A decision the reader also wrote a note about"
    statuses = {"first": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    resolution["note_regions"] = [
        {
            "page": 14,
            "bbox": [132.6, 224.5, 480.9, 299.9],
            "stroke_ids": ["page-14-line-180", "page-14-line-181"],
            "label": "A card the reader answered and wrote beside",
            "path": "/tmp/notes/page-014-note-1.png",
        }
    ]

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-03",
    )
    report = consumer.render_report(result)

    assert len(result.notes) == 1
    assert "UNREAD: 1 handwritten note(s)" in report
    assert "NOTE page 14 (2 strokes) -> /tmp/notes/page-014-note-1.png" in report


def test_a_document_with_no_handwriting_reports_no_notes(tmp_path):
    title = "A decision with nothing written beside it"
    statuses = {"first": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)

    result = consumer.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state,
        document_date="2026-09-03",
    )

    assert result.notes == []
    assert "UNREAD" not in consumer.render_report(result)


def test_a_parked_row_whose_deferral_this_page_recorded_is_skipped_on_re_detection(tmp_path):
    """mc-q2pgy: a card deferred by pen, consumed, then parked (moved out of
    Active by hand) must not fail the whole run when the same document is
    detected again; a card never recorded from this page still must."""
    title = "Show the current backup state from the laptop"
    statuses = {"hal_step": ("deferred", "DEFER")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    first = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
    )
    assert first.kept_item_ids == [item_id_for(title)]

    # Park the row by hand: remove it from Active.
    text = queue.read_text(encoding="utf-8")
    active, closed = text.split("## Closed", 1)
    active = "\n".join(line for line in active.splitlines() if not line.startswith(f"| {title} |"))
    queue.write_text(active + "\n## Closed" + closed, encoding="utf-8")

    second = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
    )
    assert second.closed_item_ids == [] and second.kept_item_ids == []

    # A parked row with NO record from this page still fails closed.
    other_manifest, other_resolution = artifacts(title, {"hal_step": ("answered", "YES")})
    other_manifest["page_id"] = other_resolution["page_id"] = "decisions-2026-09-03-other"
    with pytest.raises(consumer.DecisionConsumerV3Error, match="no longer Active"):
        consumer.consume(
            other_manifest, other_resolution, queue_path=queue, state_path=state,
            document_date="2026-09-03", apply=True, notifier=lambda _m, _c: None,
        )


# ---------------------------------------------------------------------------
# mc-9sy2v root cause 1: the v3 consumer never wrote receipts_dir at all.
# ---------------------------------------------------------------------------


def test_answered_item_writes_a_receipt(tmp_path):
    title = "A simple yes"
    statuses = {"go": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    item_id = item_id_for(title)

    result = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
        receipts_dir=receipts,
    )

    assert result.answered_item_ids == [item_id]
    receipt_path = receipts / f"{manifest['page_id']}.{item_id}.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["tool"] == "consume_decisions"
    assert receipt["page_id"] == manifest["page_id"]
    assert receipt["item_id"] == item_id
    assert receipt["row_title"] == title
    assert [a["choice"] for a in receipt["answers"]] == ["YES"]
    assert receipt["answers"][0]["entry_id"] == resolution["decisions"][0]["entry_id"]


def test_deferred_item_writes_no_receipt(tmp_path):
    title = "A deferred question"
    statuses = {"go": ("deferred", "DEFER")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)

    result = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
        receipts_dir=receipts,
    )

    assert result.answered_item_ids == []
    # receipts_dir is never even created when nothing on the page was answered.
    assert not receipts.exists() or list(receipts.glob("*.json")) == []


def test_mixed_card_writes_exactly_one_receipt(tmp_path):
    """One entry answered, one deferred, on the SAME row: exactly one receipt,
    not one per entry and not one per row-question."""
    title = "A mixed card"
    statuses = {"first": ("answered", "YES"), "second": ("deferred", "DEFER")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    item_id = item_id_for(title)

    consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
        receipts_dir=receipts,
    )

    written = list(receipts.glob("*.json"))
    assert [p.name for p in written] == [f"{manifest['page_id']}.{item_id}.json"]
    receipt = json.loads(written[0].read_text())
    assert len(receipt["answers"]) == 1
    assert receipt["answers"][0]["choice"] == "YES"


def test_rerunning_the_same_resolution_does_not_duplicate_or_corrupt_the_receipt(tmp_path):
    title = "A rerun decision"
    statuses = {"go": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    receipts = tmp_path / "receipts"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    item_id = item_id_for(title)

    first = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
        receipts_dir=receipts,
    )
    receipt_path = receipts / f"{manifest['page_id']}.{item_id}.json"
    first_receipt = receipt_path.read_bytes()
    with queue.open("a", encoding="utf-8") as queue_file:
        queue_file.write("\nUnrelated later queue edit.\n")
    replay_input = (queue.read_bytes(), state.read_bytes())

    second = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
        receipts_dir=receipts,
    )
    second_receipt = receipt_path.read_bytes()

    # The row closed on the first run; a rerun of the SAME resolution finds
    # it already recorded and closes nothing further (mirrors
    # test_a_parked_row_whose_deferral_this_page_recorded_is_skipped_on_re_detection).
    assert first.closed_item_ids == [item_id]
    assert second.closed_item_ids == []
    assert second.answered_item_ids == [item_id]
    assert list(receipts.glob("*.json")) == [receipt_path]
    assert first_receipt == second_receipt
    assert (queue.read_bytes(), state.read_bytes()) == replay_input

    tampered = json.loads(second_receipt)
    tampered["answers"][0]["choice"] = "NO"
    receipt_path.write_text(json.dumps(tampered))
    with pytest.raises(consumer.DecisionConsumerV3Error, match="refusing to overwrite"):
        consumer.consume(
            manifest, resolution, queue_path=queue, state_path=state,
            document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
            receipts_dir=receipts,
        )
    assert json.loads(receipt_path.read_text()) == tampered


def test_answered_item_with_no_receipts_dir_fails_closed_before_writing_anything(tmp_path):
    title = "A row with nowhere to record the answer"
    statuses = {"go": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    before_queue = queue.read_bytes()

    with pytest.raises(consumer.DecisionConsumerV3Error, match="receipts_dir"):
        consumer.consume(
            manifest, resolution, queue_path=queue, state_path=state,
            document_date="2026-09-02", apply=True, notifier=lambda _m, _c: None,
        )

    assert queue.read_bytes() == before_queue
    assert not state.exists()


def test_dry_run_reports_which_receipts_it_would_write(tmp_path):
    title = "A dry run row"
    statuses = {"go": ("answered", "YES")}
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    write_queue(queue, title, list(statuses))
    manifest, resolution = artifacts(title, statuses)
    item_id = item_id_for(title)

    result = consumer.consume(
        manifest, resolution, queue_path=queue, state_path=state,
        document_date="2026-09-02", apply=False,
    )

    assert result.applied is False
    assert result.answered_item_ids == [item_id]
    report = consumer.render_report(result)
    assert "receipts due (would be written on apply) for 1 row(s)" in report
    assert f"{manifest['page_id']}.{item_id}" in report
    # A dry run writes nothing outside its own temporary directory.
    assert not state.exists()


def test_ready_sheet_records_clear_answers_even_when_other_ink_needs_review(tmp_path, monkeypatch):
    title = "Mixed pen marks"
    statuses = {
        "yes": ("answered", "YES"),
        "later": ("deferred", "DEFER"),
        "unclear": ("needs_review", None),
    }
    queue = tmp_path / "REVIEW-QUEUE.md"
    state = tmp_path / "question-state.json"
    day = tmp_path / "2026-09-02"
    processed = day / "processed"
    receipts = tmp_path / "receipts"
    processed.mkdir(parents=True)
    write_queue(queue, title, list(statuses))
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "Fixture"],
                 ["config", "user.email", "fixture@example.com"], ["add", str(queue)],
                 ["commit", "-qm", "Fixture queue"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    remote = tmp_path.parent / (tmp_path.name + "-remote.git")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=tmp_path, check=True)
    subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=tmp_path, check=True)
    manifest, resolution = artifacts(title, statuses)
    manifest_path = day / "decisions-2026-09-02.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    resolution_path = processed / "resolution.json"
    resolution_path.write_text(json.dumps(resolution))
    device_path = "/Briefs/decisions-2026-09-02"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"python": sys.executable, "pages": {device_path: {
        "manifest": str(manifest_path), "out_dir": str(processed),
        "receipts_dir": str(receipts), "state": str(tmp_path / "action-state.json"),
        "review_queue": str(queue), "question_state": str(state), "apply": False,
    }}}))
    rmapi = tmp_path / "rmapi"
    rmapi.write_text("#!/bin/sh\ntouch fixture.rmdoc\n")
    rmapi.chmod(0o755)
    archive_handler = tmp_path / "archive-result.py"
    archive_handler.write_text(
        "import json, sys\n"
        f"print(json.dumps({{'status': 'needs_review', 'schema_version': 2, "
        f"'resolution': {str(resolution_path)!r}, 'apply': False}}))\n"
        "sys.exit(4)\n"
    )
    # Simulate only the tablet transport and the archive reader; the consumer and
    # its Git persistence run through their normal entry points.
    monkeypatch.setattr(today_watch, "SCRIPT", archive_handler)
    monkeypatch.setenv("REMARKABLE_ALERT_DRY_RUN", "1")
    monkeypatch.setenv("REMARKABLE_QUEUE_COMMIT", "1")

    result, = today_watch.handle_changed([device_path], registry, rmapi)
    assert result["returncode"] == 4
    assert result["status"] == "needs_review"
    saved = qs.load_state(state)["entries"]
    assert {key: saved[consumer.question_entry_id(item_id_for(title), key)]["state"]
            for key in statuses} == {
                "yes": "answered", "later": "deferred", "unclear": "needs_clarification",
            }
    assert result["consumer"]["commit"]["pushed"]
    assert "question-state.json" in subprocess.check_output(
        ["git", "ls-tree", "--name-only", "origin/main"], cwd=tmp_path, text=True
    ).splitlines()
    before = (state.read_bytes(), {p.name: p.read_bytes() for p in receipts.glob("*.json")})
    assert len(before[1]) == 1
    result, = today_watch.handle_changed([device_path], registry, rmapi)
    assert result["returncode"] == 4
    assert (state.read_bytes(), {p.name: p.read_bytes() for p in receipts.glob("*.json")}) == before
