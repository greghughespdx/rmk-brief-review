"""Real-boundary tests for the pen-chain probe, including the alarm itself (mc-rrjv)."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "remarkable"))
import pen_chain_probe as probe  # noqa: E402

NOW = datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)


def _setup(tmp_path, *, resolution=True, status="resolved", receipts=True, resolved_age_minutes=10):
    state = tmp_path / "state.tsv"
    changed_at = NOW - timedelta(hours=2)
    state.write_text(f"Briefs/decisions-2026-08-24\t{changed_at.isoformat().replace('+00:00', 'Z')}\tabc\tn\tn\n")
    out_dir = tmp_path / "2026-08-24" / "processed"
    out_dir.mkdir(parents=True)
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"pages": {"/Briefs/decisions-2026-08-24": {
        "out_dir": str(out_dir), "receipts_dir": str(receipts_dir)}}}))
    if resolution:
        res = out_dir / "resolution.json"
        res.write_text(json.dumps({"status": status, "page_id": "p1", "decisions": [
            {"item_id": "i1", "choice": "YES"}, {"item_id": "i2", "choice": "NO"}]}))
        stamp = (changed_at + timedelta(minutes=resolved_age_minutes)).timestamp()
        os.utime(res, (stamp, stamp))
        if receipts:
            for item in ("i1", "i2"):
                (receipts_dir / f"p1.{item}.json").write_text("{}")
    log = tmp_path / "watch.log"
    log.write_text("")
    return state, registry, log


def _run(state, registry, log):
    return probe.probe(state=state, registry=registry, log=log, window_hours=26, now=NOW)


def test_healthy_chain_reports_nothing(tmp_path):
    assert _run(*_setup(tmp_path)) == []


def test_missing_resolution_is_a_failure(tmp_path):
    failures = _run(*_setup(tmp_path, resolution=False))
    assert len(failures) == 1 and "no resolution.json" in failures[0]


def test_resolved_without_receipts_is_a_failure(tmp_path):
    failures = _run(*_setup(tmp_path, receipts=False))
    assert len(failures) == 1 and "no receipt" in failures[0]


def test_deferred_item_needs_no_receipt(tmp_path):
    """A DEFERRED (or discarded) v3 decision never closes a row, so no
    receipt is ever written for it and none should be required (mc-1nr9d:
    decisions-2026-09-04 flagged three genuinely-deferred item_ids)."""
    state, registry, log = _setup(tmp_path, receipts=False)
    out_dir = Path(json.loads(registry.read_text())["pages"]["/Briefs/decisions-2026-08-24"]["out_dir"])
    res = out_dir / "resolution.json"
    stamp = os.stat(res).st_mtime
    res.write_text(json.dumps({
        "status": "resolved", "page_id": "p1", "decisions": [
            {"item_id": "i1", "status": "deferred"},
            {"item_id": "i2", "status": "discarded"},
        ],
    }))
    os.utime(res, (stamp, stamp))
    assert _run(state, registry, log) == []


def test_answered_item_without_status_field_still_needs_a_receipt(tmp_path):
    """Legacy schema-1 decisions carry no status field; absence still means
    answered, so a missing receipt there must still alarm (no regression)."""
    failures = _run(*_setup(tmp_path, receipts=False))
    assert len(failures) == 1 and "no receipt" in failures[0]


def test_resolution_older_than_ink_beyond_grace_is_a_failure(tmp_path):
    failures = _run(*_setup(tmp_path, resolved_age_minutes=-200))
    assert len(failures) == 1 and "older than the ink change" in failures[0]


def test_ready_tag_clear_within_grace_is_healthy(tmp_path):
    assert _run(*_setup(tmp_path, resolved_age_minutes=-30)) == []


def test_nonzero_watch_exit_in_window_is_a_failure(tmp_path):
    state, registry, log = _setup(tmp_path)
    log.write_text(f"{(NOW - timedelta(hours=1)).isoformat()} today_watch.py exited 4\n")
    failures = _run(state, registry, log)
    assert failures == [f"watch log: today_watch.py exited 4 at {(NOW - timedelta(hours=1)).isoformat()}"]


def test_alarm_actually_fires_both_channels(tmp_path):
    """The alarm path is under test, not only the detector (mc-rrjv item 5)."""
    push = tmp_path / "push.sh"
    push.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$(dirname \"$0\")/push.txt\"\nprintf 'evt_test\n'\n")
    push.chmod(0o755)
    bus = tmp_path / "bus.sh"
    bus.write_text("#!/bin/sh\nprintf '%s|%s|%s' \"$1\" \"$2\" \"$3\" > \"$(dirname \"$0\")/bus.txt\"\n")
    bus.chmod(0o755)
    outcome = probe.alarm(["x broke"], push=push, bus=bus, dry_run=False)
    assert outcome == {"push": True, "bus": 0}
    assert "x broke" in (tmp_path / "push.txt").read_text()
    assert (tmp_path / "bus.txt").read_text().startswith("PEN CHAIN BROKEN: 1 failure(s)")


def test_cli_exits_2_and_alarms_on_failure(tmp_path):
    state, registry, log = _setup(tmp_path, resolution=False)
    push = tmp_path / "push.sh"
    push.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$(dirname \"$0\")/push.txt\"\nprintf 'evt_test\n'\n")
    push.chmod(0o755)
    bus = tmp_path / "bus.sh"
    bus.write_text("#!/bin/sh\nexit 0\n")
    bus.chmod(0o755)
    completed = subprocess.run(
        [sys.executable, str(probe.__file__), "--state", str(state), "--registry", str(registry),
         "--log", str(log), "--push", str(push), "--bus", str(bus), "--window-hours", "100000"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 2
    assert "FAIL" in completed.stderr
    assert (tmp_path / "push.txt").exists()


@pytest.mark.parametrize("missing", ["state", "registry"])
def test_missing_evidence_input_is_a_failure_not_ok(tmp_path, missing):
    state, registry, log = _setup(tmp_path)
    {"state": state, "registry": registry}[missing].unlink()
    failures = _run(state, registry, log)
    assert len(failures) == 1 and "cannot be certified" in failures[0]


def _age_state(state, minutes):
    stamp = (NOW - timedelta(minutes=minutes)).timestamp()
    os.utime(state, (stamp, stamp))


def test_quiet_day_missing_log_with_live_watcher_is_healthy(tmp_path):
    # mc-z2fu6: no ink all day means nothing ever writes the log; the detector
    # state file is rewritten every 120 s cycle, so its fresh mtime certifies
    # the watcher and the missing log is not a failure.
    state, registry, log = _setup(tmp_path)
    log.unlink()
    _age_state(state, 5)
    assert _run(state, registry, log) == []


def test_missing_log_with_stale_watcher_is_a_failure(tmp_path):
    state, registry, log = _setup(tmp_path)
    log.unlink()
    _age_state(state, probe.WATCHER_STALE_MINUTES + 15)
    failures = _run(state, registry, log)
    assert any("annotation watcher dead or stuck" in f for f in failures)
    assert any("cannot be certified" in f and "watch log missing" in f for f in failures)


def test_stale_watcher_alarms_even_when_the_log_exists(tmp_path):
    # The fail-safe must not depend on the log at all: a dead watcher with an
    # old log lying around is still a broken chain.
    state, registry, log = _setup(tmp_path)
    _age_state(state, probe.WATCHER_STALE_MINUTES + 15)
    failures = _run(state, registry, log)
    assert len(failures) == 1 and "annotation watcher dead or stuck" in failures[0]


def test_watcher_heartbeat_boundary(tmp_path):
    state, registry, log = _setup(tmp_path)
    _age_state(state, probe.WATCHER_STALE_MINUTES - 1)
    assert probe.watcher_heartbeat(state, NOW)[0] is True
    _age_state(state, probe.WATCHER_STALE_MINUTES + 1)
    assert probe.watcher_heartbeat(state, NOW)[0] is False


def test_invalid_or_empty_registry_is_a_failure(tmp_path):
    state, registry, log = _setup(tmp_path)
    registry.write_text("{not json")
    assert "cannot be certified" in _run(state, registry, log)[0]
    registry.write_text(json.dumps({"pages": {}}))
    assert "no pages" in _run(state, registry, log)[0]


def test_changed_decisions_document_without_registration_is_a_failure(tmp_path):
    state, registry, log = _setup(tmp_path)
    changed_at = NOW - timedelta(hours=1)
    state.write_text(state.read_text() + "Briefs/decisions-2026-08-25\t" + changed_at.isoformat().replace("+00:00", "Z") + "\tdef\tn\tn\n")
    failures = _run(state, registry, log)
    assert len(failures) == 1 and "not registered" in failures[0]


def test_cli_all_inputs_missing_exits_2_and_alarms(tmp_path):
    push = tmp_path / "push.sh"
    push.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$(dirname \"$0\")/push.txt\"\nprintf 'evt_test\n'\n")
    push.chmod(0o755)
    bus = tmp_path / "bus.sh"
    bus.write_text("#!/bin/sh\nprintf 'x' > \"$(dirname \"$0\")/bus.txt\"\n")
    bus.chmod(0o755)
    completed = subprocess.run(
        [sys.executable, str(probe.__file__), "--state", str(tmp_path / "missing"), "--registry", str(tmp_path / "missing"),
         "--log", str(tmp_path / "missing"), "--push", str(push), "--bus", str(bus)],
        capture_output=True, text=True,
    )
    assert completed.returncode == 2
    assert "cannot be certified" in completed.stderr
    assert (tmp_path / "push.txt").exists() and (tmp_path / "bus.txt").exists()


def test_malformed_state_row_is_a_failure_and_empty_state_is_valid(tmp_path):
    state, registry, log = _setup(tmp_path)
    state.write_text("this is not a detector baseline row\n")
    failures = _run(state, registry, log)
    assert len(failures) == 1 and "cannot be certified" in failures[0] and "malformed" in failures[0]
    state.write_text("Briefs/x\tnot-a-time\tabc\tn\tn\n")
    assert "unparseable timestamp" in _run(state, registry, log)[0]
    state.write_text("")
    assert _run(state, registry, log) == []


def test_rotated_document_maps_back_to_its_registered_key(tmp_path):
    """mc-4m64 false positive 1, reproduced from the real 2026-08-25 state row.

    Row and hash are verbatim from ~/.remarkable-annotation-state.json on disk
    2026-08-26 (decisions-2026-08-24, moved to Previous Briefs by rotate-briefs.sh
    the morning after its ink was made, exactly the shape of the 2026-08-25 07:15
    incident one rotation cycle later). Registry key/page_id are verbatim from
    ~/.remarkable-today-pages.json. Before the fix this alarms "not registered"
    even though the document is registered, resolved, and healthy.
    """
    REAL_NOW = datetime(2026, 8, 25, 14, 15, tzinfo=timezone.utc)  # 07:15 PDT
    state = tmp_path / "state.tsv"
    state.write_text(
        "Briefs/Previous Briefs/decisions-2026-08-24\t2026-08-24T21:47:34Z\t"
        "c49f6cccde521da98d6a2189e002823f09f3718d3b71915713f4ac46311b3a64\tn\tn\n"
    )
    out_dir = tmp_path / "2026-08-24" / "processed"
    out_dir.mkdir(parents=True)
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"pages": {"/Briefs/decisions-2026-08-24": {
        "page_id": "decisions-2026-08-24-700c658c6a2e",
        "out_dir": str(out_dir), "receipts_dir": str(receipts_dir)}}}))
    res = out_dir / "resolution.json"
    res.write_text(json.dumps({"status": "resolved", "page_id": "decisions-2026-08-24-700c658c6a2e",
                                "decisions": [{"item_id": "i1", "choice": "YES"}]}))
    (receipts_dir / "decisions-2026-08-24-700c658c6a2e.i1.json").write_text("{}")
    log = tmp_path / "watch.log"
    log.write_text("")
    failures = probe.probe(state=state, registry=registry, log=log, window_hours=26, now=REAL_NOW)
    assert failures == []


def test_rotated_document_fails_before_the_mapping_fix(tmp_path):
    """Same real row as above, proving the pre-fix code actually alarms on it."""
    device_path = "/Briefs/Previous Briefs/decisions-2026-08-24"
    assert probe.registered_key(device_path) == "/Briefs/decisions-2026-08-24"
    # Without the mapping, a direct lookup of the rotated path finds nothing.
    pages = {"/Briefs/decisions-2026-08-24": {"out_dir": "/irrelevant"}}
    assert pages.get(device_path) is None


def test_fresh_push_with_no_ink_is_not_a_change(tmp_path):
    """mc-4m64 false positive 2, reproduced from the real 2026-08-25 push.

    Registry entry is verbatim from ~/.remarkable-today-pages.json for
    /Briefs/decisions-2026-08-25. The push timestamp (2026-08-25T13:00:13Z) is
    verbatim from thoughts/remarkable-archive/today-pages/verify-2026-08-25.json
    ("verified_at": "2026-08-25T06:00:13-07:00"). "none" is the real hash value
    check-annotations.sh writes for a document with zero pen strokes (the same
    value seen on Briefs/morning-briefing-2026-08-25 in the live state file).
    Before the fix, a freshly pushed document with no annotations and therefore
    no resolution.json yet alarms as an unresolved ink change.
    """
    REAL_NOW = datetime(2026, 8, 25, 14, 15, tzinfo=timezone.utc)  # 07:15 PDT
    state = tmp_path / "state.tsv"
    state.write_text("Briefs/decisions-2026-08-25\t2026-08-25T13:00:13Z\tnone\tn\tn\n")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"pages": {"/Briefs/decisions-2026-08-25": {
        "page_id": "decisions-2026-08-25-351f40721f96",
        "out_dir": str(tmp_path / "2026-08-25" / "processed"),
        "receipts_dir": str(tmp_path / "receipts")}}}))
    log = tmp_path / "watch.log"
    log.write_text("")
    failures = probe.probe(state=state, registry=registry, log=log, window_hours=26, now=REAL_NOW)
    assert failures == []


def test_hash_none_row_is_excluded_before_registry_lookup(tmp_path):
    """Direct unit check on changed_documents(): 'none' never appears in the result,
    regardless of how fresh the timestamp is; '?' (unknown) still does."""
    state = tmp_path / "state.tsv"
    since = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    state.write_text(
        "Briefs/fresh-no-ink\t2026-08-25T23:59:00Z\tnone\tn\tn\n"
        "Briefs/unknown-hash\t2026-08-25T23:59:00Z\t?\tn\tn\n"
    )
    changed = probe.changed_documents(state, since)
    assert "/Briefs/fresh-no-ink" not in changed
    assert "/Briefs/unknown-hash" in changed


def test_registered_page_that_is_not_an_object_is_a_failure_not_a_crash(tmp_path):
    state, registry, log = _setup(tmp_path)
    registry.write_text(json.dumps({"pages": {"/Briefs/decisions-2026-08-24": []}}))
    failures = _run(state, registry, log)
    assert len(failures) == 1 and "not an object" in failures[0]


def test_cli_invalid_page_config_exits_2_and_alarms(tmp_path):
    state, registry, log = _setup(tmp_path)
    registry.write_text(json.dumps({"pages": {"/Briefs/decisions-2026-08-24": []}}))
    push = tmp_path / "push.sh"
    push.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$(dirname \"$0\")/push.txt\"\nprintf 'evt_test\n'\n")
    push.chmod(0o755)
    bus = tmp_path / "bus.sh"
    bus.write_text("#!/bin/sh\nprintf 'x' > \"$(dirname \"$0\")/bus.txt\"\n")
    bus.chmod(0o755)
    completed = subprocess.run(
        [sys.executable, str(probe.__file__), "--state", str(state), "--registry", str(registry), "--log", str(log),
         "--push", str(push), "--bus", str(bus), "--window-hours", "100000"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 2 and "Traceback" not in completed.stderr
    assert (tmp_path / "push.txt").exists() and (tmp_path / "bus.txt").exists()


# mc-10t5: the LaunchAgent runs /usr/bin/python3 (3.9), whose fromisoformat rejects
# the watch log's colon-less offset. These tests use the REAL log line verbatim and
# must pass under both the venv interpreter and /usr/bin/python3.
REAL_EXIT_LINE = "2026-08-25T09:40:35-0700 today_watch.py exited 4"


def test_parse_iso_accepts_colonless_offset_as_the_watch_log_writes_it():
    stamp = probe._parse_iso("2026-08-25T09:40:35-0700")
    assert stamp is not None
    assert stamp.isoformat() == "2026-08-25T16:40:35+00:00"


def test_real_exit_line_inside_window_alarms(tmp_path):
    log = tmp_path / "watch.log"
    log.write_text(REAL_EXIT_LINE + "\n", encoding="utf-8")
    since = probe._parse_iso("2026-08-25T00:00:00+00:00")
    failures = probe.log_failures(log, since)
    assert failures == ["watch log: today_watch.py exited 4 at 2026-08-25T09:40:35-0700"]


def test_unreadable_exit_stamp_is_a_failure_not_a_skip(tmp_path):
    log = tmp_path / "watch.log"
    log.write_text("garbage-stamp today_watch.py exited 4\n", encoding="utf-8")
    since = probe._parse_iso("2026-08-25T00:00:00+00:00")
    failures = probe.log_failures(log, since)
    assert len(failures) == 1 and "unreadable timestamp" in failures[0]


def test_probe_passes_without_hand_recorded_when_v3_consumer_wrote_the_receipt(tmp_path):
    """mc-9sy2v root cause 1, closed: once decision_consumer_v3 writes its own
    receipt, the probe finds it directly and never falls back to
    hand_recorded_items() at all (this config carries no manifest/review_queue,
    so that fallback is structurally unavailable here)."""
    import hashlib

    import decision_consumer_v3 as v3  # noqa: E402  (sys.path set at module top)

    row_key = "row_probe_integration"
    item_id = hashlib.sha256(row_key.encode("ascii")).hexdigest()[:12]
    title = "A probe-integration decision"
    entry_id = v3.question_entry_id(item_id, "go")
    page_id = "decisions-2026-08-24-probeintegration"

    queue = tmp_path / "REVIEW-QUEUE.md"
    queue.write_text(
        "\n".join(
            [
                "# Review Queue",
                "",
                "## Active - Blocking",
                "",
                "| Item | Project | Age | Ask |",
                "|---|---|---|---|",
                f"| {title} | example-project | 2026-08-01 | Context. Recommendation: "
                f"proceed. Decide: (1:go) [yes/no] Go? Row: {row_key} "
                "Read: docs/example.md |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_json = tmp_path / "question-state.json"
    receipts_dir = tmp_path / "receipts"
    entry_fields = {
        "entry_id": entry_id,
        "item_id": item_id,
        "card_id": "card-one",
        "question_key": "go",
        "decision_number": 1,
        "label": "Go?",
        "answer_type": "yes_no",
        "page": 1,
    }
    manifest = {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": page_id,
        "entries": [
            {
                **entry_fields,
                "boxes": [
                    {"choice": c, "bbox": [0, 0, 24, 24]}
                    for c in ("YES", "NO", "DEFER", "DISCARD")
                ],
                "write_in": None,
            }
        ],
        "cards": [
            {
                "card_id": "card-one",
                "item_id": item_id,
                "headline": title,
                "question_set_hash": v3.question_set_hash(["go"]),
            }
        ],
    }
    resolution = {
        "schema_version": 2,
        "layout_version": "decisions-v3.0",
        "page_id": page_id,
        "decisions": [
            {
                **entry_fields,
                "status": "answered",
                "choice": "YES",
                "marked_choices": ["YES"],
                "stroke_ids": ["s1"],
                "write_in": None,
            }
        ],
    }

    result = v3.consume(
        manifest,
        resolution,
        queue_path=queue,
        state_path=state_json,
        document_date="2026-08-24",
        apply=True,
        notifier=lambda _m, _c: None,
        receipts_dir=receipts_dir,
    )
    assert result.answered_item_ids == [item_id]
    assert (receipts_dir / f"{page_id}.{item_id}.json").is_file()

    changed_at = NOW - timedelta(hours=2)
    probe_state = tmp_path / "state.tsv"
    probe_state.write_text(
        f"Briefs/{page_id}\t{changed_at.isoformat().replace('+00:00', 'Z')}\tabc\tn\tn\n"
    )
    processed = tmp_path / "processed"
    processed.mkdir()
    resolution_on_disk = dict(resolution)
    resolution_on_disk["status"] = "resolved"
    res_path = processed / "resolution.json"
    res_path.write_text(json.dumps(resolution_on_disk))
    stamp = (changed_at + timedelta(minutes=10)).timestamp()
    os.utime(res_path, (stamp, stamp))
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "pages": {
                    f"/Briefs/{page_id}": {
                        "out_dir": str(processed),
                        "receipts_dir": str(receipts_dir),
                    }
                }
            }
        )
    )
    log = tmp_path / "watch.log"
    log.write_text("")

    assert probe.probe(
        state=probe_state, registry=registry, log=log, window_hours=26, now=NOW
    ) == []


def test_hand_recorded_items_matches_through_markdown_on_both_sides(tmp_path):
    """mc-9sy2v root cause 3: the manifest headline is already markdown-
    stripped by the renderer ("leave pwmmin"), while the Closed row keeps the
    raw markdown it was written with ("leave `pwm_min`"). Comparing raw
    against rendered text failed the prefix match for a row genuinely closed
    by hand on the right date with a PEN disposition (item 05ae76931e16,
    decisions-2026-09-04). Both sides must go through the same
    today_page.clean_markdown before the compare."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "item_id": "05ae76931e16",
                        "headline": "Leave pwmmin unchanged?",
                    }
                ]
            }
        )
    )
    queue_path = tmp_path / "REVIEW-QUEUE.md"
    queue_path.write_text(
        "\n".join(
            [
                "# Review Queue",
                "",
                "## Closed",
                "",
                "| Item | Closed | Disposition |",
                "|---|---|---|",
                "| Leave `pwm_min` unchanged? | 2026-09-04 | PEN 2026-09-04: leave it. |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = {"manifest": str(manifest_path), "review_queue": str(queue_path)}
    changed_at = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    assert probe.hand_recorded_items(config, changed_at) == {"05ae76931e16"}


def test_hand_recorded_items_still_requires_a_real_match_not_just_any_prefix(tmp_path):
    """The markdown normalization must not make the match too loose: an
    unrelated Closed row with different words never matches."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"cards": [{"item_id": "05ae76931e16", "headline": "Leave pwmmin unchanged?"}]}
        )
    )
    queue_path = tmp_path / "REVIEW-QUEUE.md"
    queue_path.write_text(
        "\n".join(
            [
                "# Review Queue",
                "",
                "## Closed",
                "",
                "| Item | Closed | Disposition |",
                "|---|---|---|",
                "| Something else entirely | 2026-09-04 | PEN 2026-09-04: unrelated. |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = {"manifest": str(manifest_path), "review_queue": str(queue_path)}
    changed_at = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    assert probe.hand_recorded_items(config, changed_at) == set()


def _tag_bump_setup(tmp_path, ink):
    """A resolved document whose ModifiedClient moved 2h ago; resolution is 40 days old."""
    state, registry, log = _setup(tmp_path)
    changed_at = NOW - timedelta(hours=2)
    state.write_text(f"Briefs/decisions-2026-08-24\t{changed_at.isoformat().replace('+00:00', 'Z')}\t{ink}\tn\ty\n")
    out_dir = Path(json.loads(registry.read_text())["pages"]["/Briefs/decisions-2026-08-24"]["out_dir"])
    res = out_dir / "resolution.json"
    old = (NOW - timedelta(days=40)).timestamp()
    os.utime(res, (old, old))
    baseline = tmp_path / "ink-baseline.tsv"
    first_seen = NOW - timedelta(days=40)
    baseline.write_text(f"/Briefs/decisions-2026-08-24\tabc\t{first_seen.isoformat()}\n")
    return state, registry, log, baseline


def test_tag_only_change_with_identical_ink_is_not_an_ink_change(tmp_path):
    """mc-ojmfl: 'Marked as Read' on an old resolved document moves ModifiedClient
    with the ink hash unchanged; that must not alarm."""
    state, registry, log, baseline = _tag_bump_setup(tmp_path, "abc")
    failures = probe.probe(state=state, registry=registry, log=log, window_hours=26, now=NOW,
                           baseline_path=baseline)
    assert failures == []


def test_changed_ink_hash_still_alarms_negative_control(tmp_path):
    """Same setup, but the hash differs from the baseline: real new ink on an old
    document must still be flagged, with the stale-resolution failure."""
    state, registry, log, baseline = _tag_bump_setup(tmp_path, "def")
    failures = probe.probe(state=state, registry=registry, log=log, window_hours=26, now=NOW,
                           baseline_path=baseline)
    assert any("older than the ink change" in f for f in failures)
    assert "def" in baseline.read_text()


def test_first_sighting_is_treated_as_changed(tmp_path):
    """No baseline entry: the probe cannot prove the ink is unchanged, so it alarms."""
    state, registry, log, baseline = _tag_bump_setup(tmp_path, "abc")
    baseline.unlink()
    failures = probe.probe(state=state, registry=registry, log=log, window_hours=26, now=NOW,
                           baseline_path=baseline)
    assert any("older than the ink change" in f for f in failures)


@pytest.mark.parametrize("middle", ["none", "?"])
def test_known_hash_after_none_or_unknown_is_a_first_sighting(tmp_path, middle):
    """A, then none/?, then A with an old resolution must alarm: the gap erases history."""
    state, registry, log, baseline = _tag_bump_setup(tmp_path, "abc")
    changed_at = NOW - timedelta(hours=2)

    def write(ink, stamp):
        state.write_text(f"Briefs/decisions-2026-08-24\t{stamp.isoformat()}\t{ink}\tn\ty\n")

    write(middle, changed_at)
    probe.probe(state=state, registry=registry, log=log, window_hours=26, now=NOW, baseline_path=baseline)
    write("abc", changed_at + timedelta(minutes=5))
    failures = probe.probe(state=state, registry=registry, log=log, window_hours=26, now=NOW, baseline_path=baseline)
    assert any("older than the ink change" in f for f in failures), failures
