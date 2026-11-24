"""mc-vvn7r: October 16 repeat render, lost morning history and C64 pipes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "remarkable"))
import today_page as tp
import decision_consumer_v3 as consumer

DAY = "2026-10-16"
PAGE = "decisions-2026-10-16-9891012222ec"


@pytest.fixture
def loop(tmp_path):
    archive = tmp_path / "archive"
    day = archive / DAY
    processed = day / "processed"
    processed.mkdir(parents=True)
    manifest = day / f"decisions-{DAY}.manifest.json"
    original = {
        "page_id": PAGE,
        "cards": [{"card_id": "original-card"}],
        "entries": [{"kind": "decision", "entry_id": "answered-question",
                     "item_id": "original-row", "question_key": "approve",
                     "label": "Approve the morning choice?"}],
    }
    manifest.write_text(json.dumps(original))
    (day / f"decisions-{DAY}.pdf").write_bytes(b"morning PDF")
    resolution = processed / "resolution.json"
    resolution.write_text(json.dumps({"page_id": PAGE, "decisions": []}))
    state = tmp_path / "question-state.json"
    state.write_text(json.dumps({"schema_version": 1, "entries": {
        "answered-question": {"state": "answered", "since": DAY,
                              "source": PAGE, "detail": "Answered YES."}}}))
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"pages": {f"/Briefs/decisions-{DAY}": {
        "page_id": PAGE}}}))
    calls = tmp_path / "renders.log"
    verify = tmp_path / "verify.py"
    # Only the live verification service and PDF/device builder are replaced.
    # The actual shell gates, state, manifest archive and history lookup run.
    verify.write_text("""import pathlib, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
pathlib.Path(args['--out-queue']).write_text('verified queue')
pathlib.Path(args['--report']).write_text('{}')
""")
    push = tmp_path / "push.sh"
    push.write_text("""#!/bin/sh
exec python3 - <<'STUB'
import json, os
from pathlib import Path
manifest = Path(os.environ['FIXTURE_MANIFEST'])
doc = json.loads(manifest.read_text())
log = Path(os.environ['FIXTURE_CALLS'])
count = len(log.read_text().splitlines()) + 1 if log.exists() else 1
with log.open('a') as f: print(count, file=f)
doc['page_id'] = 'decisions-2026-10-16-followup-' + str(count)
doc['entries'][0]['label'] = 'A different followup choice?'
manifest.write_text(json.dumps(doc))
STUB
""")
    push.chmod(0o755)
    env = {**os.environ, "DECISIONS_VERIFY_QUEUE": str(verify),
           "DECISIONS_PUSH_DECISIONS": str(push),
           "FIXTURE_MANIFEST": str(manifest), "FIXTURE_CALLS": str(calls)}

    def render():
        return subprocess.run([
            "bash", str(ROOT / "remarkable/decisions-loop.sh"), "render",
            "--date", DAY, "--dry-run", "--fixture-dir", str(tmp_path / "device"),
            "--archive-root", str(archive), "--question-state", str(state),
            "--registry", str(registry)], env=env, text=True, capture_output=True)

    return {"render": render, "archive": archive, "day": day,
            "original": original, "manifest": manifest, "resolution": resolution,
            "registry": registry, "state": state, "calls": calls}


def test_same_day_repeat_render_keeps_consumed_identity(loop):
    first = loop["render"]()
    assert first.returncode == 0, first.stderr
    second = loop["render"]()
    assert second.returncode == 0, second.stderr
    assert loop["calls"].read_text().splitlines() == ["1", "2"]
    saved = loop["day"] / f"consumed-{PAGE}.manifest.json"
    assert json.loads(saved.read_text()) == loop["original"]
    assert json.loads((loop["day"] / "loop-state.json").read_text())["consumed_page_id"] == PAGE


def test_next_morning_history_finds_morning_manifest_after_intraday_render(loop):
    rendered = loop["render"]()
    assert rendered.returncode == 0, rendered.stderr
    # The current file now says something different, so choosing it is wrong.
    assert json.loads(loop["manifest"].read_text())["entries"][0]["label"] != "Approve the morning choice?"
    history = tp.load_settled_question_history(
        json.loads(loop["state"].read_text()), archive_root=loop["archive"])
    assert history["answered-question"] == {
        "item_id": "original-row", "question_key": "approve",
        "question": "Approve the morning choice?"}


def test_delivered_followup_requires_its_own_consumption_and_model_answer(loop):
    assert loop["render"]().returncode == 0
    followup = json.loads(loop["manifest"].read_text())["page_id"]
    loop["registry"].write_text(json.dumps({"pages": {f"/Briefs/decisions-{DAY}": {
        "page_id": followup}}}))
    refused = loop["render"]()
    assert refused.returncode == 11, refused.stderr
    assert loop["calls"].read_text().splitlines() == ["1"]
    # New ink is consumed, but its clarification still needs a model answer.
    loop["resolution"].write_text(json.dumps({"page_id": followup, "decisions": []}))
    loop["state"].write_text(json.dumps({"schema_version": 1, "entries": {
        "new-question": {"state": "needs_clarification", "since": DAY,
                         "source": followup, "detail": "Why this choice?"}}}))
    refused = loop["render"]()
    assert refused.returncode == 12, refused.stderr
    assert loop["calls"].read_text().splitlines() == ["1"]
    (loop["day"] / "answers-complete.json").write_text(json.dumps({
        "tool": "rmw-decisions-answers-v1", "page_id": followup,
        "handled": ["clarification:new-question"]}))
    rendered = loop["render"]()
    assert rendered.returncode == 0, rendered.stderr
    assert loop["calls"].read_text().splitlines() == ["1", "2"]


def test_mismatched_resolution_still_refuses_render(loop):
    loop["resolution"].write_text(json.dumps({"page_id": "some-other-page"}))
    refused = loop["render"]()
    assert refused.returncode == 11, refused.stderr
    assert not loop["calls"].exists()
    assert json.loads(loop["manifest"].read_text()) == loop["original"]


@pytest.mark.parametrize("with_specimen", [True, False], ids=["real-c64-pipe", "no-pipe"])
def test_c64_answer_consumption_preserves_whole_ask(tmp_path, with_specimen):
    row = (ROOT / "tests/fixtures/remarkable/c64-specimen-row.md").read_text().strip()
    if not with_specimen:
        row = row.replace(" Specimen: tests/fixtures/remarkable/specimen-font/Vera.ttf | PRESS PLAY ON TAPE", "")
        assert "Specimen:" not in row
    queue = tmp_path / "REVIEW-QUEUE.md"
    queue.write_text("# Queue\n\n## Active\n\n| Item | Project | Age | Ask |\n|---|---|---|---|\n" + row +
                     "\n\n## Closed\n\n| Item | Closed | Disposition |\n|---|---|---|\n")
    # Build the manifest through the actual renderer's parser, then consume it.
    decisions = tp.parse_review_queue(queue, render_date=DAY)
    manifest = tp.build_manifest(DAY, [], decisions, {})
    entry = manifest["entries"][0]
    resolution = {"schema_version": 2, "layout_version": manifest["layout_version"],
                  "page_id": manifest["page_id"], "decisions": [{**entry,
                  "status": "answered", "choice": "YES", "marked_choices": ["YES"],
                  "stroke_ids": ["ink-1"], "write_in": None}]}
    state = tmp_path / "question-state.json"
    result = consumer.consume(manifest, resolution, queue_path=queue, state_path=state,
                              document_date=DAY, apply=True, notifications_enabled=False,
                              receipts_dir=tmp_path / "receipts")
    expected_id = hashlib.sha256(b"c64_font_install").hexdigest()[:12]
    assert result.closed_item_ids == [expected_id]
    assert json.loads(state.read_text())["entries"][entry["entry_id"]]["state"] == "answered"
    assert "c64_font_install" in queue.read_text().split("## Closed", 1)[1]
    if with_specimen:
        assert "PRESS PLAY ON TAPE" in queue.read_text()
