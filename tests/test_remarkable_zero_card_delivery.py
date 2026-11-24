"""Real render/push entry points, with outbound delivery replaced by a recorder."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "remarkable"
sys.path.insert(0, str(TOOLS))
import today_page


@pytest.fixture
def inputs(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    queue = tmp_path / "queue.md"
    report = tmp_path / "verify.json"
    return archive, queue, report


@pytest.fixture
def pipeline(tmp_path):
    isolated = tmp_path / "stack/remarkable"
    isolated.mkdir(parents=True)
    shutil.copyfile(Path(os.environ.get("N20L1_PUSH_SCRIPT", TOOLS / "push-decisions.sh")), isolated / "push-decisions.sh")
    for source in ["today_page.py", "review-receipt.py", "rmw-env.sh", "rmw_config.py", "hooks"]:
        (isolated / source).symlink_to(TOOLS / source)
    for outbound in ["replace-decisions-document.sh", "rotate-decisions.sh"]:
        program = isolated / outbound
        program.write_text('#!/bin/sh\necho called >> "$N20L1_OUTBOUND_LOG"\n')
        program.chmod(0o700)
    return isolated / "push-decisions.sh"


def queue_inputs(inputs, kind):
    archive, queue, report = inputs
    title = "Choose the sample"
    row = "| Choose the sample | test | 2026-10-20 | The sample is ready. Recommendation: approve it. Decide: (1:a) [yes/no] Go ahead? Row: sample Read: tests/fixtures/remarkable/reading-sample.md |\n"
    queue.write_text("## Active\n\n| Item | Project | Age | Ask |\n|---|---|---|---|\n" + (row if kind == "cards" else ""))
    report.write_text(json.dumps({"verified_at": "2026-10-20T06:00:00-07:00", "rows":
        [] if kind == "empty" else [{"title": title, "ok": kind == "cards", "reasons": []}]}))


@pytest.mark.parametrize("kind,expected", [("empty", 5), ("held", 3), ("cards", 4)])
def test_real_push_distinguishes_empty_held_and_unreviewed_cards(inputs, pipeline, tmp_path, kind, expected):
    queue_inputs(inputs, kind)
    archive, queue, report = inputs
    outbound_log = tmp_path / "outbound.log"
    environment = dict(os.environ, REMARKABLE_TODAY_ARCHIVE=str(archive), TODAY_EXTRACTOR_PYTHON=sys.executable,
                       N20L1_OUTBOUND_LOG=str(outbound_log))
    run = subprocess.run(["bash", str(pipeline), "--date", "2026-10-20",
        "--queue", str(queue), "--verify-report", str(report), "--push"], env=environment,
        capture_output=True, text=True, timeout=30)
    assert run.returncode == expected, run.stdout + run.stderr
    manifests = list(archive.rglob("*.manifest.json"))
    assert len(manifests) == 1
    assert bool(json.loads(manifests[0].read_text())["cards"]) is (kind == "cards")
    assert bool(list(archive.rglob("*.pdf"))) is (kind == "cards")
    assert not list(archive.rglob("review-receipt.json"))
    assert not outbound_log.exists()
    if kind == "cards": assert "REVIEW GATE" in run.stderr
    if kind == "held": assert "EMPTY-DOC TRIPWIRE" in run.stderr


def test_zero_card_generation_never_calls_pdf_renderer(inputs, tmp_path, monkeypatch):
    queue_inputs(inputs, "empty")
    archive, queue, report = inputs
    def forbidden(*args):
        raise AssertionError("The empty document reached PDF rendering")
    monkeypatch.setattr(today_page, "render_pdf", forbidden)
    pdf, manifest = today_page.generate(queue, None, report, tmp_path / "out", "2026-10-20",
        answer_record_root=archive, state_path=tmp_path / "question-state.json")
    assert pdf is None and json.loads(manifest.read_text())["cards"] == []
    assert not list((tmp_path / "out").glob("*.pdf"))


@pytest.mark.parametrize("held,expected", [([], 5), ([{"title": "Waiting item"}], 3)])
def test_existing_zero_card_manifest_does_not_require_receipt_or_pdf(inputs, pipeline, held, expected):
    archive, queue, report = inputs
    day = archive / "2026-10-20"
    day.mkdir()
    (day / "decisions-2026-10-20.manifest.json").write_text(json.dumps({"cards": [], "held": held}))
    run = subprocess.run(["bash", str(pipeline), "--date", "2026-10-20",
        "--no-generate", "--push"], env=dict(os.environ, REMARKABLE_TODAY_ARCHIVE=str(archive)),
        capture_output=True, text=True, timeout=10)
    assert run.returncode == expected, run.stdout + run.stderr
    assert not list(day.glob("*.pdf")) and not list(day.glob("*receipt*"))


@pytest.mark.parametrize("cards", [None, "", 0])
def test_unreadable_card_count_is_not_reported_as_no_decisions(inputs, pipeline, cards):
    archive, queue, report = inputs
    day = archive / "2026-10-20"
    day.mkdir()
    (day / "decisions-2026-10-20.manifest.json").write_text(json.dumps({"cards": cards}))
    run = subprocess.run(["bash", str(pipeline), "--date", "2026-10-20", "--no-generate", "--push"],
        env=dict(os.environ, REMARKABLE_TODAY_ARCHIVE=str(archive)), capture_output=True, text=True, timeout=10)
    assert run.returncode == 2
    assert "nothing to ask" not in run.stderr


def test_renderer_crash_cannot_be_reported_as_no_decisions(inputs, pipeline):
    queue_inputs(inputs, "empty")
    archive, queue, report = inputs
    generator = pipeline.with_name("today_page.py")
    generator.unlink()
    generator.write_text("raise SystemExit(5)\n")
    run = subprocess.run(["bash", str(pipeline), "--date", "2026-10-20", "--queue", str(queue),
        "--verify-report", str(report), "--push"], capture_output=True, text=True,
        env=dict(os.environ, REMARKABLE_TODAY_ARCHIVE=str(archive), TODAY_EXTRACTOR_PYTHON=sys.executable), timeout=10)
    assert run.returncode == 6
    assert "nothing to ask" not in run.stderr


def test_answered_but_active_rows_are_empty_in_both_delivery_paths(inputs, pipeline):
    queue_inputs(inputs, "cards")
    archive, queue, report = inputs
    with queue.open("a") as handle:
        handle.write("\n## Pen dispositions - 2026-10-19 (applied by consume_decisions)\n\n- **YES** Go ahead? - row: sample\n")
    env = dict(os.environ, REMARKABLE_TODAY_ARCHIVE=str(archive), TODAY_EXTRACTOR_PYTHON=sys.executable)
    common = ["bash", str(pipeline), "--date", "2026-10-20", "--push"]
    generated = subprocess.run(common + ["--queue", str(queue), "--verify-report", str(report)],
        env=env, capture_output=True, text=True, timeout=30)
    manifest = json.loads(next(archive.rglob("*.manifest.json")).read_text())
    assert manifest["cards"] == [] and manifest["held"] == []
    assert len(manifest["excluded_already_answered"]) == 1
    assert manifest["unresolved_active_titles"] == []
    existing = subprocess.run(common + ["--no-generate"], env=env,
        capture_output=True, text=True, timeout=10)
    assert generated.returncode == existing.returncode == 5
    assert not list(archive.rglob("*.pdf"))


def test_unaccounted_active_rows_remain_a_tripwire_in_both_paths(inputs, pipeline):
    queue_inputs(inputs, "held")
    archive, queue, report = inputs
    payload = json.loads(report.read_text())
    payload["rows"][0]["ok"] = True
    report.write_text(json.dumps(payload))
    env = dict(os.environ, REMARKABLE_TODAY_ARCHIVE=str(archive), TODAY_EXTRACTOR_PYTHON=sys.executable)
    common = ["bash", str(pipeline), "--date", "2026-10-20", "--push"]
    generated = subprocess.run(common + ["--queue", str(queue), "--verify-report", str(report)],
        env=env, capture_output=True, text=True, timeout=30)
    existing = subprocess.run(common + ["--no-generate"], env=env,
        capture_output=True, text=True, timeout=10)
    assert generated.returncode == existing.returncode == 3
