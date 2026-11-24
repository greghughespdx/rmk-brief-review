#!/usr/bin/env python3
"""Route changed reMarkable paths through registered Today-page manifests.

The annotation watcher passes changed device paths in the CHANGED environment
variable. With no registry, or no matching path, this program performs no work.
Registry entries default to dry-run processing unless "apply" is exactly true.

mc-1jw9: today_page.py process-archive returns a distinct nonzero exit code
per failure class (see EXIT_* there) and sends its own bus alert for the
process-archive outcomes it understands. This wrapper's job is narrower - do
not collapse that distinct code into a flat "2" (the old `check=True` bug),
and alert on its OWN failure classes (a bad registry entry, a failed rmapi
download) which today_page.py never sees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from today_page import EXIT_NEEDS_REVIEW, send_bus_alert  # noqa: E402  (local import, path set above)

import rmw_config  # noqa: E402

_CONFIG = rmw_config.load()
DEFAULT_REGISTRY = _CONFIG.registry
DEFAULT_RMAPI = Path(_CONFIG.get("tablet.rmapi"))
SCRIPT = Path(__file__).with_name("today_page.py")

# mc-1jw9: distinct from today_page.py's own EXIT_* range (0,2-5) so a code
# alone tells you which layer failed.
EXIT_OK = 0
EXIT_WATCH_ERROR = 2


class WatchError(ValueError):
    pass


CONSUMER = Path(os.environ.get("REMARKABLE_CONSUMER", str(SCRIPT.parent / "consume_decisions.py")))
REPO_ROOT = _CONFIG.content_root


EXIT_CONSUMER_FAILED = 6


def _consumer_failed(outcome: dict[str, Any]) -> bool:
    if not outcome.get("ran"):
        return outcome.get("reason") != "document not resolved"
    if outcome.get("returncode", 1) != 0:
        return True
    commit = outcome.get("commit")
    if commit is None:
        return False  # commit step disabled by REMARKABLE_QUEUE_COMMIT=0
    if not commit.get("committed") and commit.get("reason") != "nothing to commit":
        return True
    if commit.get("committed") and not commit.get("pushed"):
        return True
    return False


def _consumer_failure_text(outcome: dict[str, Any]) -> str:
    if not outcome.get("ran"):
        return f"consumer did not run: {outcome.get('reason')}"
    if outcome.get("returncode", 1) != 0:
        return f"consumer exited {outcome.get('returncode')}: {outcome.get('stderr') or outcome.get('stdout')}"
    commit = outcome.get("commit") or {}
    if not commit.get("committed"):
        return f"queue commit failed (answers written but not committed; local state kept): {commit.get('reason')}"
    return f"queue committed locally but push failed: {commit.get('push_stderr')}"


def consume_resolved(
    result: dict[str, Any], config: dict[str, Any], python: str
) -> dict[str, Any]:
    """Record clear pen answers on the review queue, then commit.

    Returns a small outcome record; never raises, because process-archive has
    already produced its artifact and an alert, and a consumer failure must be
    visible in this run's result rather than abort the other pages.
    """
    schema_two = result.get("schema_version") == 2
    partial = schema_two and result.get("returncode") == EXIT_NEEDS_REVIEW
    if (result.get("status") != "resolved" and not partial) or not result.get("resolution"):
        return {"ran": False, "reason": "document not resolved"}
    processed_dir = Path(str(result["resolution"])).expanduser().parent
    command = [python, str(CONSUMER), "--processed-dir", str(processed_dir), "--apply"]
    if config.get("review_queue"):
        command.extend(["--review-queue", str(Path(config["review_queue"]).expanduser())])
    if config.get("receipts_dir"):
        command.extend(["--receipts-dir", str(Path(config["receipts_dir"]).expanduser())])
    question_state = None
    if schema_two:
        question_state = Path(
            config.get("question_state") or _CONFIG.question_state
        ).expanduser()
        command.extend(["--question-state", str(question_state)])
    if partial:
        command.append("--partial-page-reviewed-separately")
    if os.environ.get("REMARKABLE_ALERT_DRY_RUN") == "1":
        command.append("--no-alert")
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
    except OSError as exc:
        return {"ran": False, "reason": f"consumer could not start: {exc}"}
    outcome: dict[str, Any] = {
        "ran": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip()[-2000:],
        "stderr": completed.stderr.strip()[-2000:],
    }
    if completed.returncode == 0 and os.environ.get("REMARKABLE_QUEUE_COMMIT", "1") == "1":
        outcome["commit"] = commit_queue(
            Path(config.get("review_queue") or _CONFIG.queue).expanduser(),
            Path(config.get("receipts_dir") or "").expanduser() if config.get("receipts_dir") else None,
            processed_dir,
            str(result.get("device_path", "")),
            question_state=question_state,
        )
    return outcome


def commit_queue(
    queue_path: Path, receipts_dir: Path | None, processed_dir: Path, device_path: str,
    *, question_state: Path | None = None,
) -> dict[str, Any]:
    """Commit the consumer's queue and receipt writes so the answers persist."""
    repo = _git_root(queue_path)
    if repo is None:
        return {"committed": False, "reason": "queue file is not inside a git repository"}
    paths = [str(queue_path)]
    for extra in (receipts_dir, processed_dir, question_state):
        if extra and extra.exists() and extra.resolve().is_relative_to(repo):
            paths.append(str(extra))
    try:
        subprocess.run(["git", "-C", str(repo), "add", "--", *paths], check=True, capture_output=True, text=True)
        staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", *paths], capture_output=True)
        if staged.returncode == 0:
            return {"committed": False, "reason": "nothing to commit"}
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", f"pen answers consumed: {device_path}",
             "--only", "--", *paths],
            check=True, capture_output=True, text=True,
        )
        push = subprocess.run(["git", "-C", str(repo), "push", "-q"], capture_output=True, text=True)
        return {"committed": True, "pushed": push.returncode == 0, "push_stderr": push.stderr.strip()[-300:]}
    except subprocess.CalledProcessError as exc:
        return {"committed": False, "reason": (exc.stderr or str(exc)).strip()[-300:]}


def _git_root(path: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"pages": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("pages", {}), dict):
        raise WatchError("Today-page registry must contain a pages object.")
    return value


def handle_changed(
    changed: list[str],
    registry_path: Path = DEFAULT_REGISTRY,
    rmapi: Path = DEFAULT_RMAPI,
) -> list[dict[str, Any]]:
    registry = load_registry(registry_path)
    pages = registry.get("pages", {})
    python = str(registry.get("python", sys.executable))
    results: list[dict[str, Any]] = []
    for device_path in changed:
        # The detector emits paths without a leading slash; the registry keys
        # with one. Accept both (found live 2026-07-28, first qualification).
        config = pages.get(device_path)
        if config is None and not device_path.startswith("/"):
            device_path = "/" + device_path
            config = pages.get(device_path)
        if config is None:
            continue
        if not isinstance(config, dict):
            raise WatchError(f"Registry entry for {device_path} must be an object.")
        required = ("manifest", "out_dir", "receipts_dir", "state")
        missing = [key for key in required if not config.get(key)]
        if missing:
            raise WatchError(f"Registry entry for {device_path} is missing {missing}.")
        with tempfile.TemporaryDirectory(prefix="remarkable-today-") as directory:
            subprocess.run(
                [str(rmapi), "get", device_path],
                cwd=directory,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            archives = [path for path in Path(directory).iterdir() if path.is_file()]
            if len(archives) != 1:
                raise WatchError(f"Expected one downloaded archive for {device_path}.")
            command = [
                python,
                str(SCRIPT),
                "process-archive",
                "--manifest",
                str(Path(config["manifest"]).expanduser()),
                "--archive",
                str(archives[0]),
                "--out-dir",
                str(Path(config["out_dir"]).expanduser()),
                "--receipts-dir",
                str(Path(config["receipts_dir"]).expanduser()),
                "--state",
                str(Path(config["state"]).expanduser()),
            ]
            if config.get("jobs"):
                command.extend(["--jobs", str(Path(config["jobs"]).expanduser())])
            if config.get("review_queue"):
                command.extend(
                    ["--review-queue", str(Path(config["review_queue"]).expanduser())]
                )
            if config.get("apply") is True:
                command.append("--apply")
            if os.environ.get("REMARKABLE_ALERT_DRY_RUN") == "1":
                command.append("--alert-dry-run")
            # mc-1jw9 defect 2/5: no check=True here. A nonzero exit from
            # process-archive is a real, already-alerted outcome (blocked,
            # needs-review, no-ink-read, crashed) - collapsing it into a
            # CalledProcessError erased its distinct code and, combined with
            # this loop previously aborting on the first exception, silently
            # skipped every OTHER registered page changed in the same run.
            completed = subprocess.run(
                command, check=False, text=True, capture_output=True
            )
            result = None
            if completed.returncode == EXIT_OK:
                result = json.loads(completed.stdout)
            elif completed.returncode == EXIT_NEEDS_REVIEW:
                try:
                    partial_result = json.loads(completed.stdout)
                except json.JSONDecodeError:
                    partial_result = None
                if isinstance(partial_result, dict) and partial_result.get("schema_version") == 2:
                    result = partial_result
            if result is not None:
                result["device_path"] = device_path
                result["returncode"] = completed.returncode
                # A schema-2 partial result still carries the clear decisions.
                # The consumer records them and holds ambiguous entries, while
                # the archive's needs-review exit continues to reach the watcher.
                result["consumer"] = consume_resolved(result, config, python)
                if _consumer_failed(result["consumer"]):
                    # A resolved document whose answers did not persist is a
                    # failed handling, not a success with a footnote: surface
                    # it as this page's outcome so main() exits nonzero and
                    # the watcher alert fires.
                    result["returncode"] = EXIT_CONSUMER_FAILED
                    result["status"] = "consumer_failed"
                    result["stderr"] = _consumer_failure_text(result["consumer"])
                    send_bus_alert(
                        f"reMarkable pen answers NOT recorded: {device_path}",
                        result["stderr"],
                        "remarkable-consumer-failed",
                        dry_run=os.environ.get("REMARKABLE_ALERT_DRY_RUN") == "1",
                    )
                results.append(result)
            else:
                results.append(
                    {
                        "device_path": device_path,
                        "returncode": completed.returncode,
                        "status": "process_archive_failed",
                        "stderr": completed.stderr.strip(),
                    }
                )
    return results


def main() -> int:
    changed = [line for line in os.environ.get("CHANGED", "").splitlines() if line.strip()]
    registry_path = Path(
        os.environ.get("REMARKABLE_TODAY_REGISTRY", str(DEFAULT_REGISTRY))
    ).expanduser()
    rmapi = Path(os.environ.get("REMARKABLE_RMAPI", str(DEFAULT_RMAPI))).expanduser()
    alert_dry_run = os.environ.get("REMARKABLE_ALERT_DRY_RUN") == "1"
    try:
        results = handle_changed(changed, registry_path, rmapi)
    except (OSError, json.JSONDecodeError, WatchError, subprocess.CalledProcessError) as exc:
        print(f"Today-page handling stopped safely: {exc}", file=sys.stderr)
        send_bus_alert(
            "reMarkable Decisions watcher: could not run process-archive",
            f"{exc}\n\nCHANGED={os.environ.get('CHANGED', '')}",
            "remarkable-watch-error",
            dry_run=alert_dry_run,
        )
        return EXIT_WATCH_ERROR
    if results:
        print(json.dumps(results, sort_keys=True, separators=(",", ":")))
    failed = [entry for entry in results if entry.get("returncode", 0) != 0]
    if failed:
        for entry in failed:
            print(
                f"Today-page handling: process-archive exited "
                f"{entry['returncode']} for {entry['device_path']}: "
                f"{entry.get('stderr', '')}",
                file=sys.stderr,
            )
        # process-archive already sent its own alert for this outcome
        # (mc-1jw9 defect 2) - this wrapper only needs to not swallow the
        # exit code. Propagate the worst (highest) code seen.
        return max(entry["returncode"] for entry in failed)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
