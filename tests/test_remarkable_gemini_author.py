"""Exercise the Decisions author with a local agy stub and the real verifier."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTHOR = ROOT / "remarkable/writer/author.py"
READ = "tests/fixtures/remarkable/reading-sample.md"
ROW = (
    "| Library hours | Library | 2026-11-23 | The library can close its drop box later for one week. "
    "Recommendation: Yes. Decide: (1:later) [yes/no] Should the library try the later hours? "
    f"Recommended: Yes because it needs no extra staff. Row: library_hours Read: {READ} |"
)
HEADER = "# Review Queue\n\n## Active - Decisions\n\n"


@pytest.fixture
def scratch(tmp_path):
    reading = tmp_path / READ
    reading.parent.mkdir(parents=True)
    reading.write_text((ROOT / READ).read_text())
    queue = tmp_path / "queue.md"
    queue.write_text(HEADER + "| Item | Project | Age | Ask |\n|---|---|---|---|\n" + ROW +
                     "\n\n## Closed\n" + ROW.replace("library_hours", "closed_card") + "\n")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"schema_version": 1, "entries": {}}))
    (tmp_path / "decisions").mkdir()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"seed": {}, "rewritten": {"library_hours": "0" * 64}}))
    return queue, state, ledger


def agy_stub(tmp_path, *, outcome="ok"):
    exe = tmp_path / "agy"
    seen = tmp_path / "model-called"
    card = {
        "key": "library_hours", "headline": "Try later library drop box hours?",
        "lead": "A one-week trial needs no extra staff.",
        "questions": [{"key": "later", "form": "[yes/no]", "context": "",
                       "question": "Should the library try closing its drop box later for one week?",
                       "recommended": "Yes because readers get more time."}],
    }
    if outcome == "bad_form":
        card["questions"][0]["form"] = "[write-in]"
    if outcome == "held":
        card["headline"] = "Ask about an unknown ticket mc-zzyy99?"
    response = {"event": "result", "result": {"status": "SUCCESS", "response": json.dumps([card])}}
    exe.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        "content = json.loads(sys.stdin.readline())['message']['content']\n"
        "assert '\"key\": \"library_hours\"' in content and 'closed_card' not in content\n"
        "assert sys.argv[sys.argv.index('--model') + 1] == 'gemini-4-neon-high'\n"
        f"Path({str(seen)!r}).touch()\n"
        + ("sys.exit(10)\n" if outcome == "cli_failure" else f"print({json.dumps(response)!r})\n")
    )
    exe.chmod(0o700)
    return exe, seen


def author(scratch, output, exe):
    queue, state, ledger = scratch
    return subprocess.run(
        [sys.executable, AUTHOR, "--queue", queue, "--state", state, "--ledger", ledger,
         "--agy", exe, "--dry-run-out", output],
        capture_output=True, text=True, timeout=55,
    )


def test_changed_row_is_rewritten_and_verified_without_touching_input(scratch, tmp_path):
    queue, _state, _ledger = scratch
    output = tmp_path / "out.md"
    exe, seen = agy_stub(tmp_path)
    original = queue.read_bytes()
    result = author(scratch, output, exe)
    assert result.returncode == 0, result.stderr
    assert seen.is_file() and "Verified 1 rewritten row" in result.stdout
    text = output.read_text()
    assert "Try later library drop box hours?" in text
    assert f"(1:later) [yes/no]" in text and f"Row: library_hours Read: {READ}" in text
    assert text.split("## Closed")[1] == original.decode().split("## Closed")[1]
    assert queue.read_bytes() == original


@pytest.mark.parametrize("outcome", ["bad_form", "cli_failure", "held"])
def test_failed_rewrite_leaves_original_and_emits_no_candidate(scratch, tmp_path, outcome):
    output = tmp_path / "out.md"
    exe, seen = agy_stub(tmp_path, outcome=outcome)
    original = scratch[0].read_bytes()
    result = author(scratch, output, exe)
    assert seen.is_file(), result.stderr
    assert "retained" in result.stderr or "did not finish" in result.stderr
    assert not output.exists()
    assert scratch[0].read_bytes() == original


@pytest.mark.parametrize("terminal", ["answered", "discarded", "already_authored"])
def test_settled_and_unchanged_cards_never_reach_gemini(scratch, tmp_path, terminal):
    _queue, state, ledger = scratch
    if terminal == "already_authored":
        ledger.write_text(json.dumps({"seed": {}, "rewritten": {
            "library_hours": hashlib.sha256(ROW.encode()).hexdigest()
        }}))
    else:
        item = hashlib.sha256(b"library_hours").hexdigest()[:12]
        entry = hashlib.sha256(f"{item}\0later".encode()).hexdigest()[:12]
        state.write_text(json.dumps({"schema_version": 1, "entries": {entry: {
            "state": terminal, "since": "2026-11-23", "source": "fixture", "detail": "fixture",
        }}}))
    exe, seen = agy_stub(tmp_path)
    output = tmp_path / "out.md"
    result = author(scratch, output, exe)
    assert result.returncode == 0, result.stderr
    assert "No changed, unanswered" in result.stdout and not seen.exists()
    assert not output.exists()


@pytest.fixture
def publishing_repo(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("decisions_author", AUTHOR)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    repo, origin = tmp_path / "checkout", tmp_path / "origin.git"
    repo.mkdir()
    monkeypatch.setattr(module, "ROOT", repo)
    # The configured queue lives in a git checkout; these are its relative paths.
    monkeypatch.setattr(module, "QUEUE", "decisions-queue.md")
    monkeypatch.setattr(module, "LEDGER", "decisions-writer-ledger.json")
    monkeypatch.setattr(module, "STATE", "archive/question-state.json")

    def git(*args):
        return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("init", "-q", "--bare", str(origin))
    git("config", "user.email", "review-fixture@example.invalid")
    git("config", "user.name", "Fixture")
    git("remote", "add", "origin", str(origin))
    old = {module.QUEUE: ROW + "\n", module.LEDGER: "{}\n"}
    new = {module.QUEUE: ROW.replace("Library hours", "Extend library hours?") + "\n",
           module.LEDGER: '{"fixture":"digest"}\n'}
    state = b'{"schema_version":1,"entries":{}}\n'
    for name, content in {**old, module.STATE: state.decode(), "unrelated.txt": "old\n"}.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git("add", *old, module.STATE, "unrelated.txt")
    git("commit", "-qm", "fixtures")
    git("push", "-q", "origin", "main")
    return module, repo, git, old, new, state


def advance_origin(repo, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    subprocess.run(["git", "-C", other, "remote", "set-url", "origin",
                    str(tmp_path / "origin.git")], check=True)
    subprocess.run(["git", "-C", other, "-c", "user.email=review-fixture@example.invalid",
                    "-c", "user.name=Fixture", "commit", "--allow-empty", "-qm", "Concurrent update"],
                   check=True)
    subprocess.run(["git", "-C", other, "push", "-q", "origin", "main"], check=True)
    return subprocess.check_output(["git", "-C", other, "rev-parse", "HEAD"], text=True).strip()


def test_commit_only_publishes_verified_queue_and_ledger(publishing_repo):
    module, repo, git, old, new, state = publishing_repo
    head = git("rev-parse", "HEAD")
    (repo / "unrelated.txt").write_text("staged separately\n")
    git("add", "unrelated.txt")
    with pytest.raises(ValueError, match="answer state changed"):
        module.commit_updates(old, new, b"stale snapshot", head)
    assert git("rev-parse", "HEAD") == head
    assert (repo / module.QUEUE).read_text() == old[module.QUEUE]
    module.commit_updates(old, new, state, head)
    assert set(git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == set(new)
    assert git("diff", "--cached", "--name-only") == "unrelated.txt"
    assert git("rev-parse", "HEAD") == git("rev-parse", "origin/main")


def test_prepare_main_fast_forwards_stale_checkout(publishing_repo, tmp_path):
    module, repo, git, _old, _new, _state = publishing_repo
    remote_head = advance_origin(repo, tmp_path)
    assert git("rev-parse", "HEAD") == git("rev-parse", "origin/main") != remote_head
    assert module.prepare_main() == remote_head
    assert git("rev-parse", "HEAD") == git("rev-parse", "origin/main") == remote_head


def test_rejected_push_leaves_main_and_its_index_unchanged(publishing_repo, tmp_path):
    module, repo, git, old, new, state = publishing_repo
    head = module.prepare_main()
    (repo / "unrelated.txt").write_text("staged separately\n")
    git("add", "unrelated.txt")
    staged = git("diff", "--cached")
    remote_head = advance_origin(repo, tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        module.commit_updates(old, new, state, head)
    assert git("rev-parse", "HEAD") == head
    assert git("diff", "--cached") == staged
    assert git("diff") == ""
    assert all((repo / path).read_text() == content for path, content in old.items())
    assert git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert module.prepare_main() == remote_head


def test_deferred_card_is_rewritten(scratch, tmp_path):
    """A deferred card is rewritten, keys and forms unchanged."""
    _queue, state, _ledger = scratch
    item = hashlib.sha256(b"library_hours").hexdigest()[:12]
    entry = hashlib.sha256(f"{item}\0later".encode()).hexdigest()[:12]
    state.write_text(json.dumps({"schema_version": 1, "entries": {entry: {
        "state": "deferred", "since": "2026-11-23", "source": "fixture", "detail": "fixture",
    }}}))
    exe, seen = agy_stub(tmp_path)
    output = tmp_path / "out.md"
    result = author(scratch, output, exe)
    assert result.returncode == 0, result.stderr
    assert seen.exists()
