#!/usr/bin/env python3
"""One scheduled question for the whole pen chain (mc-rrjv item 3).

Pen mark on the reMarkable -> detector -> today_watch -> process-archive ->
resolution.json -> consumer -> review-queue receipts. Every component can be
green while the chain is broken (2026-08-16: seven marks hit a reader format
error, the wrapper logged errors, and nobody was told). This probe asks the
end-to-end question once a day and fails LOUDLY:

  For every registered Decisions document whose ink changed in the last
  --window hours (per the detector's own baseline file), does a fresh
  resolution.json exist, and if it resolved, does a receipt exist for each
  decision? And did the watch log record any nonzero exits in the window?

Exit 0 only when every question answers yes. Any failure sends one alert via
the page hook (urgent push) and the alert hook, and exits 2. The alarm path is
itself under test (test_pen_chain_probe.py mutation-tests the alarm, not only
the detector).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import today_page as tp  # noqa: E402  (path shim above is deliberate)

HOME = Path.home()
DEFAULT_STATE = HOME / ".remarkable-annotation-state.json"
import rmw_config  # noqa: E402

DEFAULT_REGISTRY = rmw_config.load().registry
DEFAULT_LOG = Path("/tmp/remarkable-annotation-watch.log")
# Urgent operator push (hooks.page) and operator alert (hooks.alert). Both are
# logged no-ops until configured.
DEFAULT_PUSH = Path(__file__).resolve().parent / "hooks" / "page.sh"
GRACE_MINUTES = 90
# The annotation watcher (<label_prefix>.annotation-watch) runs every
# 120 s and check-annotations.sh rewrites the detector state file on every
# successful run, changed ink or not. Its mtime is therefore the watcher's
# heartbeat. Fifteen missed cycles is dead, not busy.
WATCHER_STALE_MINUTES = 30
DEFAULT_BUS = Path(__file__).resolve().parent / "hooks" / "alert.sh"


_COMPACT_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 stamp as every writer in this chain emits it.

    The annotation watch log stamps lines with a colon-less UTC offset
    (2026-08-25T09:40:35-0700, from `date +%Y-%m-%dT%H:%M:%S%z`). Python 3.11+
    accepts that; the LaunchAgent runs /usr/bin/python3, which is 3.9 and
    rejects it. Before this normalization every exit line in the log was
    silently skipped under the interpreter that actually runs the probe, so a
    real 'today_watch.py exited 4' never alarmed (mc-4m64 follow-up).
    """
    text = value.replace("Z", "+00:00")
    text = _COMPACT_OFFSET.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ProbeInputError(Exception):
    """An evidence input is missing or unreadable: the chain cannot be certified."""


def load_ink_baseline(path: Path) -> dict[str, tuple[str, datetime]]:
    """path -> (ink hash, when the probe first saw that hash) from the probe's own file.

    mc-ojmfl: ModifiedClient moves on any save, including a tag-only change. On
    2026-10-17 the reader's "Marked as Read" tap on the six-week-old decisions-2026-09-04
    moved ModifiedClient with every pen stroke byte-identical (all 20 .rm payloads
    matched the September archive), and the probe alarmed twice on a non-ink change.
    The detector state keeps only the latest hash, so the probe keeps its own history:
    an unchanged hash keeps its first-seen stamp, so only a changed hash restarts the clock.
    """
    out: dict[str, tuple[str, datetime]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        stamp = _parse_iso(parts[2]) if len(parts) >= 3 else None
        if stamp is not None:
            out[parts[0]] = (parts[1], stamp)
    return out


def save_ink_baseline(path: Path, baseline: dict[str, tuple[str, datetime]]) -> None:
    rows = [f"{k}\t{h}\t{t.isoformat()}" for k, (h, t) in sorted(baseline.items())]
    path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


def changed_documents(state_path: Path, since: datetime,
                      baseline: dict[str, tuple[str, datetime]] | None = None) -> dict[str, datetime]:
    """Device paths whose detector baseline shows ink changed at or after `since`.

    A row's hash field (path<TAB>ModifiedClient<TAB>inkhash<TAB>ready<TAB>seen, per
    check-annotations.sh) of "none" means the document has no pen strokes at all - a
    freshly pushed document with no annotations yet. That is not an ink change no
    matter how recent its ModifiedClient stamp is (mc-4m64: the 06:00 fresh push of
    the day's Decisions document was alarming as an unresolved ink change before this
    check existed). "?" (unknown/failed read) still counts, unchanged.
    """
    changed: dict[str, datetime] = {}
    if not state_path.is_file():
        raise ProbeInputError(f"detector state missing: {state_path}")
    for number, line in enumerate(state_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue  # an empty state file is valid no-change evidence; a malformed row is not
        parts = line.split("\t")
        if len(parts) < 5:
            raise ProbeInputError(f"detector state row {number} is malformed (expected 5 tab fields): {line[:80]!r}")
        stamp = _parse_iso(parts[1].strip())
        if stamp is None:
            raise ProbeInputError(f"detector state row {number} has an unparseable timestamp: {parts[1][:40]!r}")
        key = "/" + parts[0].strip().lstrip("/")
        ink = parts[2].strip()
        if ink in ("none", "?") and baseline is not None:
            # No known strokes this cycle: drop the history so a later known hash is a
            # first sighting and cannot inherit a stale stamp (A, none, A).
            baseline.pop(key, None)
        if ink == "none":
            continue  # no ink at all: not a change, regardless of timestamp
        if baseline is not None and ink != "?":
            prior = baseline.get(key)
            if prior is not None and prior[0] == ink:
                stamp = min(stamp, prior[1])  # same strokes as before: not an ink change
            else:
                baseline[key] = (ink, stamp)  # first sighting or new ink: clock starts at ModifiedClient
        if stamp < since:
            continue
        changed[key] = stamp
    return changed


PREVIOUS_BRIEFS_PREFIX = "/Briefs/Previous Briefs/"


def registered_key(device_path: str) -> str:
    """Map a device path to the key it was registered under.

    rotate-briefs.sh moves (never renames) a document from /Briefs/<name> to
    /Briefs/Previous Briefs/<name> once it is no longer today's brief (`rmapi mv`).
    The registry keys every document by its original /Briefs/<name> path, so a
    rotated document must map back to that key for the registration check and any
    other registry lookup - otherwise every rotation morning reports a healthy,
    already-processed document as newly unregistered (mc-4m64).
    """
    if device_path.startswith(PREVIOUS_BRIEFS_PREFIX):
        return "/Briefs/" + device_path[len(PREVIOUS_BRIEFS_PREFIX):]
    return device_path


def registry_pages(registry_path: Path) -> dict[str, dict]:
    if not registry_path.is_file():
        raise ProbeInputError(f"Decisions registry missing: {registry_path}")
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProbeInputError(f"Decisions registry unreadable: {registry_path}: {exc}")
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict) or not pages:
        raise ProbeInputError(f"Decisions registry has no pages: {registry_path}")
    return pages


def check_document(device_path: str, changed_at: datetime, config: dict) -> list[str]:
    """Return the list of failures for one registered document (empty = healthy)."""
    failures: list[str] = []
    out_dir = Path(str(config.get("out_dir", ""))).expanduser()
    # The registry's out_dir is the processed directory itself for documents
    # registered by replace-decisions-document.sh, and the parent for older
    # registrations. Accept either.
    candidates = [out_dir / "resolution.json", out_dir / "processed" / "resolution.json"]
    resolution = next((c for c in candidates if c.exists()), None)
    if resolution is None:
        return [f"{device_path}: ink changed {changed_at.isoformat()} but no resolution.json under {out_dir}"]
    resolved_at = datetime.fromtimestamp(resolution.stat().st_mtime, tz=timezone.utc)
    # Clearing the ready tag after processing bumps the cloud mtime the
    # detector records, so the ink timestamp can trail the resolution by the
    # handling time. Allow that grace; anything older is a real miss.
    if resolved_at < changed_at - timedelta(minutes=GRACE_MINUTES):
        failures.append(
            f"{device_path}: resolution.json ({resolved_at.isoformat()}) is older than the ink change ({changed_at.isoformat()})"
        )
    try:
        data = json.loads(resolution.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return failures + [f"{device_path}: resolution.json unreadable: {exc}"]
    status = data.get("status")
    if status not in ("resolved", "needs_review"):
        failures.append(f"{device_path}: resolution status is {status!r}")
    if status == "resolved":
        receipts_dir = Path(str(config.get("receipts_dir", ""))).expanduser()
        page_id = str(data.get("page_id") or "")
        decisions = data.get("decisions") or []
        # A schema-1 decision carries no "status" field at all (only a
        # definitive YES/NO mark is ever recorded), so its absence still
        # means answered. A schema-2 (v3) decision names its status
        # explicitly, and "deferred"/"discarded"/"needs_review" are not
        # answers - nothing was decided, so the row never closes and a
        # receipt is never due. Requiring one there is a false positive
        # (mc-1nr9d: decisions-2026-09-04 flagged three DEFERRED item_ids
        # that were correctly never recorded).
        item_ids = {
            str(d.get("item_id"))
            for d in decisions
            if d.get("item_id") and d.get("status", "answered") == "answered"
        }
        hand_recorded = hand_recorded_items(config, changed_at)
        missing = [
            item_id for item_id in sorted(item_ids)
            if not (receipts_dir / f"{page_id}.{item_id}.json").exists()
            and item_id not in hand_recorded
        ]
        if item_ids and missing:
            failures.append(
                f"{device_path}: {len(missing)} of {len(item_ids)} answered rows have no receipt (consumer did not record): {', '.join(missing[:5])}"
            )
    return failures


def hand_recorded_items(config: dict, changed_at: datetime) -> set[str]:
    """Item ids whose queue row was closed BY HAND on or after the ink date.

    The orchestrator sometimes dispositions a document in conversation before the
    consumer runs. That is a legitimate record: the row sits in the Closed
    table with a date on or after the ink and a "PEN" disposition. The
    manifest maps each card's item_id to its title, which is how the queue
    names rows.
    """
    manifest_path = Path(str(config.get("manifest", ""))).expanduser()
    queue_path = Path(str(config.get("review_queue", ""))).expanduser()
    if not manifest_path.is_file() or not queue_path.is_file():
        return set()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    titles = {}
    for card in manifest.get("cards", []):
        item_id = str(card.get("item_id") or "")
        title = str(card.get("headline") or card.get("title") or "").strip()
        if item_id and title:
            titles[item_id] = title
    closed = _closed_rows(queue_path)
    # REVIEW-QUEUE.md dates are written from system-local timestamps
    # (dt.datetime.now().astimezone() across consume_decisions.py,
    # review-receipt.py, verify-queue.py, question_state.py -- never UTC).
    # changed_at is UTC (the detector state file's ModifiedClient stamp,
    # normalized to UTC in _parse_iso). Comparing changed_at.date() in UTC
    # against a Pacific-local closed_date rolls the ink date forward a day
    # for any evening ink change (UTC is 7-8h ahead), which makes a same-
    # evening hand-closed row look like it predates the ink and never
    # matches (mc-9sy2v: decisions-2026-09-20 ink changed 2026-09-21T00:56Z
    # = 2026-09-20T17:56 PDT, rows correctly closed 2026-09-20 by pen).
    ink_date = changed_at.astimezone().date()
    recorded = set()
    for item_id, title in titles.items():
        # The manifest headline is already markdown-stripped by the renderer
        # (backticks, underscores rendered as plain text: `pwm_min` ->
        # "pwmmin"), while the Closed row keeps the raw markdown it was
        # written with ("`pwm_min`"). Comparing raw against rendered text can
        # fail the prefix match even for a row genuinely closed by hand on
        # the right date (mc-9sy2v root cause 3: item 05ae76931e16, "gpu2 gpu
        # blower idle floor: leave pwmmin" vs "...leave `pwm_m" truncated at
        # 40 raw chars). Both sides go through the same clean_markdown the
        # renderer itself uses before anything else happens to them.
        #
        # The manifest headline keeps its trailing "?"; the review-queue row
        # title never does. Stripping it before truncating to 40 chars keeps
        # the prefix match working regardless of title length. Without the
        # strip, a title <=40 chars keeps its "?" inside the truncated key
        # and never matches the queue row, while a longer title (truncated
        # before the "?") matches fine - a false MISSING RECEIPT for any
        # short headline (mc-kzq8r: card-002 of decisions-2026-09-19,
        # "Structured SOP model for the prototype?", 39 chars).
        key = tp.clean_markdown(title).rstrip("?").strip()[:40].lower()
        for row_title, closed_date, disposition in closed:
            if (
                tp.clean_markdown(row_title).lower().startswith(key)
                and closed_date >= ink_date
                and "PEN" in disposition.upper()
            ):
                recorded.add(item_id)
                break
    return recorded


def _closed_rows(queue_path: Path) -> list[tuple[str, "date", str]]:
    from datetime import date as _date
    rows = []
    in_closed = False
    for line in queue_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_closed = line.startswith("## Closed")
            continue
        if not in_closed or not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] == "Item":
            continue
        try:
            closed_date = _date.fromisoformat(cells[1][:10])
        except ValueError:
            continue
        rows.append((cells[0], closed_date, cells[2]))
    return rows


LOG_EXIT = re.compile(r"^(\S+)\s+today_watch\.py exited (\d+)")


def watcher_heartbeat(state_path: Path, now: datetime) -> tuple[bool, datetime]:
    """(alive, last_write) from the detector state file's mtime.

    mc-z2fu6: on a day with no pen activity the watch log is never written (its
    only writers fire on a change), and /tmp is cleared on reboot, so a missing
    log is NOT evidence of a dead watcher. The state file is rewritten every
    cycle, so a recent mtime certifies the watcher ran; a stale one is the real
    dead-watcher signal and still alarms, log or no log.
    """
    if not state_path.is_file():
        raise ProbeInputError(f"detector state missing: {state_path}")
    last = datetime.fromtimestamp(state_path.stat().st_mtime, tz=timezone.utc)
    return last >= now - timedelta(minutes=WATCHER_STALE_MINUTES), last


def log_failures(log_path: Path, since: datetime, *, watcher_alive: bool = False) -> list[str]:
    if not log_path.is_file():
        if watcher_alive:
            return []  # quiet day: nothing ever wrote the log, and the watcher heartbeat is fresh
        raise ProbeInputError(f"watch log missing: {log_path}")
    failures = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LOG_EXIT.match(line)
        if not match:
            continue
        stamp = _parse_iso(match.group(1))
        if stamp is None:
            # An exit line whose stamp cannot be read is not evidence of health.
            # Skipping it is exactly how the check went inert under Python 3.9.
            failures.append(
                f"watch log: unreadable timestamp {match.group(1)!r} on a "
                f"today_watch.py exit line (exit {match.group(2)})"
            )
            continue
        if stamp < since:
            continue
        if match.group(2) != "0":
            failures.append(f"watch log: today_watch.py exited {match.group(2)} at {match.group(1)}")
    return failures


def alarm(failures: list[str], *, push: Path, bus: Path, dry_run: bool) -> dict:
    subject = f"PEN CHAIN BROKEN: {len(failures)} failure(s) in the last day"
    body = "\n".join(failures)
    outcome = {"push": None, "bus": None}
    if dry_run:
        return {"push": "dry-run", "bus": "dry-run"}
    if push.exists():
        done = subprocess.run(
            [str(push), "--title", subject, "--body", body, "--level", "critical",
             "--source", "pen_chain_probe"], capture_output=True, text=True,
        )
        outcome["push"] = bool(done.stdout.strip())
    if bus.exists():
        done = subprocess.run([str(bus), subject, body, "pen-chain-probe"], capture_output=True, text=True)
        outcome["bus"] = done.returncode
    return outcome


def probe(*, state: Path, registry: Path, log: Path, window_hours: float, now: datetime | None = None,
          baseline_path: Path | None = None, persist_baseline: bool = True) -> list[str]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    failures: list[str] = []
    try:
        pages = registry_pages(registry)
        baseline = load_ink_baseline(baseline_path) if baseline_path is not None else None
        changed = changed_documents(state, since, baseline)
        if baseline is not None and persist_baseline:
            save_ink_baseline(baseline_path, baseline)
        for device_path, changed_at in changed.items():
            config = pages.get(registered_key(device_path))
            if config is not None and not isinstance(config, dict):
                raise ProbeInputError(f"Decisions registry entry for {device_path} is not an object")
            if config is None:
                # Only Decisions documents are registered; a changed decisions-*
                # document with no registration means the chain broke at the
                # registration step, which is itself a failure.
                if "/decisions-" in device_path:
                    failures.append(f"{device_path}: ink changed but the document is not registered (chain broken at registration)")
                continue
            failures.extend(check_document(device_path, changed_at, config))
        alive, last_write = watcher_heartbeat(state, now)
        if not alive:
            failures.append(
                f"annotation watcher dead or stuck: detector state last written {last_write.isoformat()}, "
                f"more than {WATCHER_STALE_MINUTES} minutes ago"
            )
        failures.extend(log_failures(log, since, watcher_alive=alive))
    except ProbeInputError as exc:
        failures.append(f"evidence input unavailable, chain cannot be certified: {exc}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--window-hours", type=float, default=26.0)
    parser.add_argument("--push", default=str(DEFAULT_PUSH))
    parser.add_argument("--bus", default=str(DEFAULT_BUS))
    parser.add_argument("--ink-baseline", default=None,
                        help="probe's own ink-hash history (default: next to --state)")
    parser.add_argument("--dry-run", action="store_true", help="report, never alert")
    args = parser.parse_args()
    state = Path(args.state).expanduser()
    baseline = Path(args.ink_baseline).expanduser() if args.ink_baseline else state.with_name(state.name + ".probe-ink.tsv")
    failures = probe(
        state=state, registry=Path(args.registry).expanduser(),
        log=Path(args.log).expanduser(), window_hours=args.window_hours,
        baseline_path=baseline, persist_baseline=not args.dry_run,
    )
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not failures:
        print(f"{stamp} pen-chain-probe OK: every changed Decisions document in the window resolved and was recorded")
        return 0
    for failure in failures:
        print(f"{stamp} pen-chain-probe FAIL: {failure}", file=sys.stderr)
    outcome = alarm(failures, push=Path(args.push).expanduser(), bus=Path(args.bus).expanduser(), dry_run=args.dry_run)
    print(f"{stamp} pen-chain-probe alarm: {json.dumps(outcome)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
