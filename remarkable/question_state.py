#!/usr/bin/env python3
"""Durable per-question state for the Decisions document.

The state file is deliberately small and strict. A missing file means no
questions have been processed yet. A malformed or newer file is an error, not
an empty state. Callers hold ``locked_state`` across their read, plan, and
write so two consumers cannot silently overwrite one another.
"""

from __future__ import annotations

import copy
import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

import today_page as tp

SCHEMA_VERSION = 1
VALID_STATES = frozenset(
    {"unanswered", "answered", "deferred", "discarded", "needs_clarification"}
)
TERMINAL_STATES = frozenset({"answered", "discarded"})
RESOLUTION_TO_STATE = {
    "unanswered": "unanswered",
    "answered": "answered",
    "deferred": "deferred",
    "discarded": "discarded",
    "needs_review": "needs_clarification",
}


class QuestionStateError(RuntimeError):
    """The durable question state cannot be trusted or safely updated."""


def empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "entries": {}}


def _validate_record(entry_id: str, record: Any) -> None:
    if not isinstance(record, dict):
        raise QuestionStateError(f"question state {entry_id!r} is not an object")
    if record.get("state") not in VALID_STATES:
        raise QuestionStateError(f"question state {entry_id!r} has an invalid state")
    for field in ("since", "source", "detail"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise QuestionStateError(
                f"question state {entry_id!r} has no usable {field}"
            )
    notified_at = record.get("notified_at")
    if notified_at is not None and (
        not isinstance(notified_at, str) or not notified_at
    ):
        raise QuestionStateError(
            f"question state {entry_id!r} has an invalid notified_at"
        )


def validate_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise QuestionStateError("question state is not an object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        relation = "newer" if isinstance(version, int) and version > SCHEMA_VERSION else "unsupported"
        raise QuestionStateError(
            f"question state schema_version {version!r} is {relation}; no state was changed"
        )
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise QuestionStateError("question state entries are missing")
    for entry_id, record in entries.items():
        if not isinstance(entry_id, str) or not entry_id:
            raise QuestionStateError("question state contains an invalid entry id")
        _validate_record(entry_id, record)
    return payload


def load_state(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return empty_state()
    try:
        payload = tp._read_json(path)
    except Exception as exc:
        raise QuestionStateError(
            f"question state could not be read from {path}: {exc}"
        ) from exc
    validate_state(payload)
    return payload


@contextmanager
def locked_state(path: Path) -> Iterator[dict[str, Any]]:
    """Yield trusted state while holding its writer lock."""
    state_path = Path(path)
    with tp.queue_lock(state_path):
        yield load_state(state_path)


def write_state(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a validated state file. Caller must hold its lock."""
    validate_state(payload)
    tp.atomic_json(Path(path), payload)
    Path(path).chmod(0o600)


def _detail(decision: dict[str, Any]) -> str:
    status = decision["status"]
    choice = decision.get("choice")
    write_in = decision.get("write_in")
    if status == "answered" and choice:
        suffix = " with a handwritten note" if write_in else ""
        return f"Answered {choice}{suffix}."
    if status == "answered":
        return "Answered in handwriting; the crop requires human reading."
    if status == "deferred":
        return "Deferred today; ask again tomorrow."
    if status == "discarded":
        return "Discarded; never ask this question again."
    if status == "needs_review":
        return "The pen marks need clarification before any answer is recorded."
    return "No answer was marked."


def plan_resolution(
    current: dict[str, Any],
    resolution: dict[str, Any],
    *,
    resolved_on: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return updated state and entry ids needing a clarification message.

    Replaying the same page and entry is a no-op only when it says the same
    thing. A changed interpretation of the same source fails closed instead of
    silently rewriting the durable answer.
    """
    validate_state(current)
    if resolution.get("schema_version") != 2:
        raise QuestionStateError("per-question state requires resolution schema 2")
    page_id = resolution.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise QuestionStateError("resolution page_id is missing")
    decisions = resolution.get("decisions")
    if not isinstance(decisions, list):
        raise QuestionStateError("resolution decisions are missing")
    day = resolved_on or dt.date.today().isoformat()
    planned = copy.deepcopy(current)
    entries = planned["entries"]
    clarifications: list[str] = []
    seen: set[str] = set()
    for index, decision in enumerate(decisions, start=1):
        if not isinstance(decision, dict):
            raise QuestionStateError(f"resolution decision {index} is not an object")
        entry_id = decision.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            raise QuestionStateError(
                f"resolution decision {index} has a missing or repeated entry_id"
            )
        seen.add(entry_id)
        status = decision.get("status")
        target = RESOLUTION_TO_STATE.get(status)
        if target is None:
            raise QuestionStateError(
                f"resolution decision {entry_id!r} has invalid status {status!r}"
            )
        detail = _detail(decision)
        prior = entries.get(entry_id)
        if prior and prior["source"] == page_id:
            if prior["state"] != target or prior["detail"] != detail:
                raise QuestionStateError(
                    f"resolution for {entry_id!r} changed for the same source page"
                )
            if target == "needs_clarification" and not prior.get("notified_at"):
                clarifications.append(entry_id)
            continue
        if prior and prior["state"] in TERMINAL_STATES:
            # Answered and discarded are terminal. A different page cannot
            # resurrect or reinterpret them.
            continue
        if target == "unanswered" and prior is not None:
            continue
        record = {
            "state": target,
            "since": day,
            "source": page_id,
            "detail": detail,
        }
        entries[entry_id] = record
        if target == "needs_clarification":
            clarifications.append(entry_id)
    validate_state(planned)
    return planned, clarifications


def mark_notified(
    payload: dict[str, Any], entry_ids: Iterable[str], *, notified_at: str | None = None
) -> dict[str, Any]:
    """Return state with successful clarification deliveries recorded."""
    validate_state(payload)
    stamp = notified_at or dt.datetime.now().astimezone().isoformat()
    updated = copy.deepcopy(payload)
    for entry_id in entry_ids:
        record = updated["entries"].get(entry_id)
        if not record or record["state"] != "needs_clarification":
            raise QuestionStateError(
                f"cannot mark notification for non-clarification entry {entry_id!r}"
            )
        record["notified_at"] = stamp
    validate_state(updated)
    return updated
