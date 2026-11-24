from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "remarkable"))

import question_state as qs  # noqa: E402


def resolution(*decisions):
    return {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": "decisions-2026-09-02-sample",
        "decisions": list(decisions),
    }


def decision(entry_id, status, choice=None, write_in=None):
    return {
        "entry_id": entry_id,
        "status": status,
        "choice": choice,
        "write_in": write_in,
    }


def test_partial_resolution_preserves_each_question_independently():
    planned, clarifications = qs.plan_resolution(
        qs.empty_state(),
        resolution(
            decision("q-answer", "answered", "B"),
            decision("q-defer", "deferred", "DEFER"),
            decision("q-discard", "discarded", "DISCARD"),
            decision("q-empty", "unanswered"),
        ),
        resolved_on="2026-09-02",
    )

    assert clarifications == []
    assert {
        entry_id: record["state"] for entry_id, record in planned["entries"].items()
    } == {
        "q-answer": "answered",
        "q-defer": "deferred",
        "q-discard": "discarded",
        "q-empty": "unanswered",
    }


def test_discard_is_terminal_across_later_page_replays():
    first, _ = qs.plan_resolution(
        qs.empty_state(),
        resolution(decision("q", "discarded", "DISCARD")),
        resolved_on="2026-09-02",
    )
    later = resolution(decision("q", "answered", "YES"))
    later["page_id"] = "decisions-2026-09-03-sample"

    planned, _ = qs.plan_resolution(first, later, resolved_on="2026-09-03")

    assert planned["entries"]["q"] == first["entries"]["q"]


def test_changed_interpretation_of_same_source_fails_closed():
    first, _ = qs.plan_resolution(
        qs.empty_state(), resolution(decision("q", "answered", "YES"))
    )
    first["entries"]["q"]["state"] = "deferred"
    first["entries"]["q"]["detail"] = "Deferred today; ask again tomorrow."

    with pytest.raises(qs.QuestionStateError, match="changed for the same source"):
        qs.plan_resolution(first, resolution(decision("q", "answered", "YES")))


def test_same_page_cannot_change_a_terminal_interpretation():
    first, _ = qs.plan_resolution(
        qs.empty_state(), resolution(decision("q", "answered", "YES"))
    )

    with pytest.raises(qs.QuestionStateError, match="changed for the same source"):
        qs.plan_resolution(first, resolution(decision("q", "discarded", "DISCARD")))


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "entries": {}},
        {"schema_version": 1, "entries": []},
        {"schema_version": 1, "entries": {"q": {"state": "answered"}}},
    ],
)
def test_malformed_or_newer_state_never_resets_to_empty(tmp_path, payload):
    state_path = tmp_path / "question-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(qs.QuestionStateError):
        qs.load_state(state_path)

    assert json.loads(state_path.read_text(encoding="utf-8")) == payload


def test_state_write_is_atomic_and_owner_only(tmp_path):
    state_path = tmp_path / "question-state.json"
    payload, _ = qs.plan_resolution(
        qs.empty_state(), resolution(decision("q", "answered", "YES"))
    )

    with qs.locked_state(state_path):
        qs.write_state(state_path, payload)

    assert qs.load_state(state_path) == payload
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_clarification_remains_pending_until_delivery_is_recorded():
    planned, clarifications = qs.plan_resolution(
        qs.empty_state(), resolution(decision("q", "needs_review"))
    )
    assert clarifications == ["q"]

    replayed, clarifications = qs.plan_resolution(planned, resolution(decision("q", "needs_review")))
    assert clarifications == ["q"]

    notified = qs.mark_notified(
        replayed, ["q"], notified_at="2026-09-02T01:00:00-07:00"
    )
    _, clarifications = qs.plan_resolution(notified, resolution(decision("q", "needs_review")))
    assert clarifications == []
