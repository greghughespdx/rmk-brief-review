"""The watcher hands a resolved document to the consumer and commits (mc-63x2)."""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "remarkable"))
import today_watch  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path, with_remote=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "REVIEW-QUEUE.md").write_text("## Active\n")
    _git(repo, "add", "REVIEW-QUEUE.md")
    _git(repo, "commit", "-q", "-m", "init")
    if with_remote:
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _fake_consumer(tmp_path, exit_code=0):
    script = tmp_path / "consumer.py"
    script.write_text(
        "import sys, pathlib, json\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(sys.argv[0]).with_name('consumer-args.json').write_text(json.dumps(args))\n"
        "queue = args[args.index('--review-queue') + 1]\n"
        "open(queue, 'a').write('| recorded by consumer |\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    return script


def test_resolved_document_is_consumed_and_committed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    consumer = _fake_consumer(tmp_path)
    monkeypatch.setattr(today_watch, "CONSUMER", consumer)
    monkeypatch.setenv("REMARKABLE_ALERT_DRY_RUN", "1")
    processed = tmp_path / "2026-08-24" / "processed"
    processed.mkdir(parents=True)
    result = {"status": "resolved", "resolution": str(processed / "resolution.json"), "device_path": "/Briefs/decisions-2026-08-24"}
    config = {"review_queue": str(repo / "REVIEW-QUEUE.md"), "receipts_dir": str(repo / "receipts")}
    outcome = today_watch.consume_resolved(result, config, sys.executable)
    assert outcome["ran"] is True and outcome["returncode"] == 0
    args = json.loads((tmp_path / "consumer-args.json").read_text())
    assert args[:3] == ["--processed-dir", str(processed), "--apply"]
    assert "--no-alert" in args
    assert outcome["commit"]["committed"] is True
    assert outcome["commit"]["pushed"] is True, outcome["commit"]
    assert _git(repo, "rev-parse", "HEAD").stdout == _git(repo, "rev-parse", "origin/main").stdout
    log = _git(repo, "log", "--oneline").stdout
    assert "pen answers consumed: /Briefs/decisions-2026-08-24" in log
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_unresolved_document_is_not_consumed(tmp_path, monkeypatch):
    consumer = _fake_consumer(tmp_path)
    monkeypatch.setattr(today_watch, "CONSUMER", consumer)
    outcome = today_watch.consume_resolved({"status": "needs_review", "resolution": "x"}, {}, sys.executable)
    assert outcome == {"ran": False, "reason": "document not resolved"}
    assert not (tmp_path / "consumer-args.json").exists()


def test_consumer_failure_is_reported_and_nothing_is_committed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    consumer = _fake_consumer(tmp_path, exit_code=3)
    monkeypatch.setattr(today_watch, "CONSUMER", consumer)
    processed = tmp_path / "p"
    processed.mkdir()
    result = {"status": "resolved", "resolution": str(processed / "resolution.json"), "device_path": "/Briefs/x"}
    outcome = today_watch.consume_resolved(result, {"review_queue": str(repo / "REVIEW-QUEUE.md")}, sys.executable)
    assert outcome["ran"] is True and outcome["returncode"] == 3
    assert "commit" not in outcome
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 1


def _fake_process_archive(tmp_path, resolution_path):
    """A fake today_page.py: its process-archive prints a resolved result."""
    script = tmp_path / "today_page.py"
    script.write_text(
        "import json, sys\n"
        f"print(json.dumps({{'status': 'resolved', 'resolution': {str(resolution_path)!r}}}))\n"
    )
    return script


def _registry(tmp_path, repo, python_script):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"python": sys.executable, "pages": {"/Briefs/decisions-x": {
        "manifest": str(tmp_path / "m.json"), "out_dir": str(tmp_path / "out"), "receipts_dir": str(repo / "receipts"),
        "state": str(tmp_path / "state.json"), "review_queue": str(repo / "REVIEW-QUEUE.md"), "apply": True}}}))
    return reg


def _fake_rmapi(tmp_path):
    script = tmp_path / "rmapi"
    script.write_text("#!/bin/sh\ntouch ./decisions-x.rmdoc\nexit 0\n")
    script.chmod(0o755)
    return script


def test_handle_changed_wires_the_consumer_for_a_resolved_document(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    resolution = tmp_path / "out" / "resolution.json"
    resolution.parent.mkdir()
    monkeypatch.setattr(today_watch, "SCRIPT", _fake_process_archive(tmp_path, resolution))
    monkeypatch.setattr(today_watch, "CONSUMER", _fake_consumer(tmp_path))
    monkeypatch.setenv("REMARKABLE_ALERT_DRY_RUN", "1")
    results = today_watch.handle_changed(["Briefs/decisions-x"], _registry(tmp_path, repo, None), _fake_rmapi(tmp_path))
    assert len(results) == 1 and results[0]["returncode"] == 0
    assert results[0]["consumer"]["ran"] is True and results[0]["consumer"]["commit"]["pushed"] is True
    assert (tmp_path / "consumer-args.json").exists()


def test_commit_failure_is_a_failed_watcher_outcome(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    resolution = tmp_path / "out" / "resolution.json"
    resolution.parent.mkdir()
    monkeypatch.setattr(today_watch, "SCRIPT", _fake_process_archive(tmp_path, resolution))
    monkeypatch.setattr(today_watch, "CONSUMER", _fake_consumer(tmp_path))
    monkeypatch.setenv("REMARKABLE_ALERT_DRY_RUN", "1")
    results = today_watch.handle_changed(["Briefs/decisions-x"], _registry(tmp_path, repo, None), _fake_rmapi(tmp_path))
    assert results[0]["returncode"] == today_watch.EXIT_CONSUMER_FAILED
    assert results[0]["status"] == "consumer_failed"
    assert "not committed" in results[0]["stderr"]
    assert "recorded by consumer" in (repo / "REVIEW-QUEUE.md").read_text()  # local state preserved


def test_push_failure_is_a_failed_watcher_outcome(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    resolution = tmp_path / "out" / "resolution.json"
    resolution.parent.mkdir()
    monkeypatch.setattr(today_watch, "SCRIPT", _fake_process_archive(tmp_path, resolution))
    monkeypatch.setattr(today_watch, "CONSUMER", _fake_consumer(tmp_path))
    monkeypatch.setenv("REMARKABLE_ALERT_DRY_RUN", "1")
    results = today_watch.handle_changed(["Briefs/decisions-x"], _registry(tmp_path, repo, None), _fake_rmapi(tmp_path))
    assert results[0]["returncode"] == today_watch.EXIT_CONSUMER_FAILED
    assert "push failed" in results[0]["stderr"]
    assert "pen answers consumed" in _git(repo, "log", "--oneline").stdout  # committed locally


def test_consumer_nonzero_exit_is_a_failed_outcome_with_an_alert_and_no_commit(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    resolution = tmp_path / "out" / "resolution.json"
    resolution.parent.mkdir()
    monkeypatch.setattr(today_watch, "SCRIPT", _fake_process_archive(tmp_path, resolution))
    monkeypatch.setattr(today_watch, "CONSUMER", _fake_consumer(tmp_path, exit_code=5))
    monkeypatch.delenv("REMARKABLE_ALERT_DRY_RUN", raising=False)
    alerts = []
    monkeypatch.setattr(today_watch, "send_bus_alert", lambda subject, body, slug, dry_run=False: alerts.append((subject, body, slug, dry_run)) or {"sent": True})
    results = today_watch.handle_changed(["Briefs/decisions-x"], _registry(tmp_path, repo, None), _fake_rmapi(tmp_path))
    assert results[0]["returncode"] == today_watch.EXIT_CONSUMER_FAILED
    assert results[0]["status"] == "consumer_failed"
    assert "consumer exited 5" in results[0]["stderr"]
    assert len(alerts) == 1
    subject, body, slug, _ = alerts[0]
    assert subject == "reMarkable pen answers NOT recorded: /Briefs/decisions-x"
    assert "consumer exited 5" in body and slug == "remarkable-consumer-failed"
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 1  # nothing committed


def test_main_exits_nonzero_when_the_consumer_fails(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    resolution = tmp_path / "out" / "resolution.json"
    resolution.parent.mkdir()
    monkeypatch.setattr(today_watch, "SCRIPT", _fake_process_archive(tmp_path, resolution))
    monkeypatch.setattr(today_watch, "CONSUMER", _fake_consumer(tmp_path, exit_code=5))
    monkeypatch.setattr(today_watch, "send_bus_alert", lambda *a, **k: {"sent": True})
    monkeypatch.setattr(today_watch, "DEFAULT_RMAPI", _fake_rmapi(tmp_path))
    monkeypatch.setenv("CHANGED", "Briefs/decisions-x")
    monkeypatch.setenv("REMARKABLE_TODAY_REGISTRY", str(_registry(tmp_path, repo, None)))
    monkeypatch.setenv("REMARKABLE_RMAPI", str(_fake_rmapi(tmp_path)))
    assert today_watch.main() == today_watch.EXIT_CONSUMER_FAILED
