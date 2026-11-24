#!/usr/bin/env python3
"""Freshness pass for the Decisions document (the reader requirement, 2026-07-28).

Nothing renders unverified: every active REVIEW-QUEUE.md row is checked before
generation, and rows that fail are HELD OFF the document, never printed stale.

Checks per row:
  1. Every beads ticket id (mc-xxxx) the row cites must still be open. A cited
     closed ticket means the row may be superseded; the row is held for the
     orchestrator to re-verify by hand.
  2. Every repo-relative markdown link and the row's "Read:" document must
     exist.
  3. With --live-cards URL (mc-0mtb0, phone-question design section 6 item 4): no
     question of the row may have a phone question card that is open,
     answered, discarded, or waiting for its second-model review. An
     answered or discarded card means the reader already answered on the phone and
     the row was not closed; an open card, or one in review (it goes to the
     phone within minutes), means the question is on his phone today. Either way the row is held, so nothing is
     asked twice and nothing is asked on the tablet and the phone the same
     day. A deferred card is reported, not held: where a deferred question
     comes back is the reader's open choice on that design (defer_return). If the
     answer service cannot be read, nothing is held for this check and the
     report says the check did not run.

Outputs:
  --out-queue   a filtered copy of REVIEW-QUEUE.md containing only verified
                active rows (held rows removed); non-active sections unchanged.
  --report      JSON verification record: per-row status, reasons, timestamp.

Exit 0 always; held rows are reported, not fatal. The wrapper feeds the
filtered copy to the generator and the document states the verification time.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rmw_config  # noqa: E402

_CONFIG = rmw_config.load()
# Links and Read: paths on a card resolve under the content root.
MC = _CONFIG.content_root

# Child tickets carry a dotted suffix (mc-pwk7o.19), and 42 of them exist in
# the tracker today. The pattern used to stop at the dot, so a row citing a
# child was checked against its PARENT: a card citing a CLOSED child passed
# verification, and a card citing an open child under a closed parent was
# held for no reason. Found 2026-09-13 when two parses of the same table
# disagreed by three tickets (mc-9jd4t).
# The ticket-id pattern comes from tickets.pattern in the config. Empty (the
# default) turns the ticket check off. The pattern below is the beads form
# (prefix-xxxx with optional dotted child suffixes), shown as an example:
#   pattern = '\bmc-[a-z0-9]{2,6}(?:\.[0-9]+)*\b'
_TICKET_PATTERN = str(_CONFIG.get("tickets.pattern") or "")
MC_ID = re.compile(_TICKET_PATTERN) if _TICKET_PATTERN else None
LINK = re.compile(r"\]\(([^)]+)\)")
# mc-iacbw: the row names its reading with "Read: <path>". The card parser
# (validate_row) holds the row when that file is missing or unreadable, with a
# reason a person can act on, so this pass only has to check the other links.
READ = re.compile(r"Read:\s*([^\s|]+)")


def load_ticket_status():
    """Current ticket states as {ticket_id: status}.

    Off unless tickets.pattern is set. When hooks.ticket_status is configured,
    that command prints the JSON object. Otherwise, for a beads tracker, a fresh
    `bd export` is read from the content root, falling back to the checked-in
    .beads/issues.jsonl snapshot. A snapshot is a commit-time copy: tickets
    created since its last commit would read as "unknown to the tracker" and
    hold real rows off the document, which is why the fresh export comes first.
    """
    import subprocess
    import tempfile

    status = {}
    if MC_ID is None:
        return status
    hook = _CONFIG.hook("ticket_status")
    if hook:
        try:
            out = subprocess.run(hook, cwd=MC, check=True, capture_output=True,
                                 text=True, timeout=60).stdout
            loaded = json.loads(out)
            if isinstance(loaded, dict):
                return {str(k): str(v) for k, v in loaded.items()}
        except Exception:
            return status
        return status
    jsonl = MC / ".beads" / "issues.jsonl"
    try:
        fresh = Path(tempfile.gettempdir()) / "verify-queue-issues-fresh.jsonl"
        subprocess.run(
            ["bd", "export", "--output", str(fresh)],
            cwd=MC,
            check=True,
            capture_output=True,
            timeout=60,
        )
        jsonl = fresh
    except Exception:
        pass
    if not jsonl.exists():
        return status
    for line in jsonl.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("id"):
            status[d["id"]] = d.get("status", "unknown")
    return status


ROW_KEY = re.compile(r"Row:\s*([^\s|]+?)\s*\.?\s*(?:\||$)")
QUESTION_KEY = re.compile(r"\((\d+):([a-z0-9_]+)\)")

# Phone card states that hold a row, with the reason printed on page 1.
LIVE_HOLD = {
    "answered": "it was already answered on the phone, and the row has not been closed yet",
    "discarded": "it was already discarded on the phone, and the row has not been closed yet",
    "open": "it is open as a question on the phone today",
    # With live review on, a card waits in 'review' until a second model passes it, then goes to the
    # phone within minutes, so it counts as on the phone today (the cross-reviewer's cross-PR finding, mc-0mtb0).
    "review": "it is waiting for review before it goes to the phone today",
}
LIVE_PRIORITY = ("answered", "discarded", "open", "review", "deferred")


def fetch_live_cards(url):
    """(cards by question id, None) from the phone-question answer service, or (None, why not).

    Token: env PHONE_CARDS_VERIFY_TOKEN, else the output of the command in
    PHONE_CARDS_TOKEN_COMMAND. The token is never printed.
    """
    import os
    import subprocess
    import urllib.request

    token = os.environ.get("PHONE_CARDS_VERIFY_TOKEN", "").strip()
    if not token:
        # Optional: a command that prints the token (a password manager CLI,
        # for example). Unset means there is no token and the check is skipped.
        command = os.environ.get("PHONE_CARDS_TOKEN_COMMAND", "").strip()
        if not command:
            return None, "no verifier token"
        import shlex
        try:
            token = subprocess.run(shlex.split(command), capture_output=True, text=True,
                                   timeout=60, check=True).stdout.strip()
        except Exception as exc:
            return None, f"no verifier token ({type(exc).__name__})"
        if not token:
            return None, "no verifier token"
    try:
        req = urllib.request.Request(url.rstrip("/") + "/v1/linked", headers={"X-Ask-Verify-Token": token})
        with urllib.request.urlopen(req, timeout=15) as r:
            cards = json.loads(r.read())["cards"]
        by_qid = {}
        for c in cards:
            by_qid.setdefault(str(c["question_id"]), []).append(c)
    except Exception as exc:
        return None, f"answer service could not be read ({type(exc).__name__})"
    return by_qid, None


def live_card_for(cells, live):
    """The phone cards that ask a question of this row: list of (question key, question id, card).

    The row key is split off the Ask the way the generator splits it ("Row:" then "Read:"
    at the end), so the question ids are the ones the generator and the answer service use.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from today_page import _entry_identity, _split_read, _split_row_key, row_item_id
    ask = re.sub(r"\s+", " ", "|".join(cells[3:])).strip()
    try:
        _ask, row_key = _split_row_key(_split_read(ask)[0])
    except Exception:  # a malformed row is already held by the card-contract check
        return []
    if not row_key:
        return []
    item_id = row_item_id(row_key)
    out = []
    for key in dict.fromkeys(k for _n, k in QUESTION_KEY.findall(ask)):
        qid = _entry_identity(item_id, key)
        cards = live.get(qid) or []
        states = [c for c in cards if c.get("status") in LIVE_PRIORITY]
        if states:
            states.sort(key=lambda c: LIVE_PRIORITY.index(c["status"]))
            out.append((key, qid, states[0]))
    return out


def _row_key(line):
    """The row's declared key, or None when it does not declare one."""
    found = ROW_KEY.search(line)
    return found.group(1) if found else None


def check_row(line, cells, tickets, render_date=None, queue_path=None):
    """Return (ok, reasons) for one active row."""
    reasons = []
    text = " | ".join(cells)
    # Card-contract check (mc-sguf/mc-fezd): validate the RAW LINE through the
    # generator's full row parser (real title/age/ask, same cell split), so a
    # row that passes verification cannot be rejected or crash at render time.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from today_page import validate_row
    queue_arg = Path(queue_path) if queue_path is not None else None
    reasons.extend(
        f"card contract: {r}"
        for r in validate_row(line, render_date=render_date, queue_path=queue_arg)
    )
    for tid in (sorted(set(MC_ID.findall(text))) if MC_ID is not None else []):
        st = tickets.get(tid)
        if st in ("closed",):
            reasons.append(f"cites {tid} which is closed - possibly superseded")
        elif st is None:
            reasons.append(f"cites {tid} which is unknown to the tracker")
    for m in LINK.findall(text) + READ.findall(text):
        path = m.strip().rstrip(".")
        if path.startswith("http"):
            continue
        candidate = (MC / path.lstrip("./")).resolve()
        # ../projects/... links resolve relative to the queue file location
        alt = (MC / "REVIEW-QUEUE.md").parent / path
        if not candidate.exists() and not alt.resolve().exists():
            reasons.append(f"referenced file missing: {path}")
    return (not reasons, reasons)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(MC / "REVIEW-QUEUE.md"))
    ap.add_argument("--out-queue", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--render-date", default=datetime.date.today().isoformat())
    ap.add_argument("--live-cards", metavar="URL",
                    help="phone-question answer service; hold rows with an open, answered or discarded phone card")
    args = ap.parse_args()

    src = Path(args.queue).read_text()
    tickets = load_ticket_status()
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    out_lines = []
    seen_row_keys: set[str] = set()
    report = {"verified_at": now, "rows": [], "held": []}
    live = None
    if args.live_cards:
        live, why = fetch_live_cards(args.live_cards)
        report["live_cards"] = {"checked": live is not None, "error": why, "held": [], "deferred": []}
    in_active = False
    for line in src.splitlines():
        if line.startswith("## "):
            in_active = line.startswith("## Active")
            out_lines.append(line)
            continue
        if in_active:
            # Row detection must match the renderer's (mc-fezd): use the
            # generator's own splitter, not a local pattern.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from today_page import _split_row
            row_cells = _split_row(line)
        else:
            row_cells = None
        if row_cells is not None:
            cells = row_cells
            title = cells[0] if cells else "?"
            ok, reasons = check_row(
                line, cells, tickets, render_date=args.render_date, queue_path=args.queue
            )
            # A row key has to be unique across Active rows or two rows share
            # one identity, and one row's answers would land on the other
            # (mc-iacbw section 1). Only this pass sees every row at once, so
            # the duplicate check lives here rather than in the card parser.
            row_key = _row_key(line)
            if row_key:
                if row_key in seen_row_keys:
                    ok = False
                    reasons = reasons + [
                        f"another Active row already uses the row key "
                        f"{row_key!r}"
                    ]
                seen_row_keys.add(row_key)
            if live:
                live_reasons = []
                for key, qid, card in live_card_for(cells, live):
                    hit = {"title": title, "question_key": key, "question_id": qid,
                           "card_id": card.get("card_id"), "status": card["status"],
                           "answer_kind": card.get("answer_kind")}
                    if card["status"] in LIVE_HOLD:
                        if LIVE_HOLD[card["status"]] not in live_reasons:
                            live_reasons.append(LIVE_HOLD[card["status"]])
                        report["live_cards"]["held"].append(hit)
                    else:
                        report["live_cards"]["deferred"].append(hit)
                if live_reasons:
                    ok = False
                    reasons = live_reasons + reasons
            # Full title, never truncated: today_page.load_verification_report
            # compares these against the queue's full titles for the
            # nothing-renders-unverified gate, and a lossy cut here silently
            # fails every long row (found live 2026-08-01, mc-sguf class).
            entry = {"title": title, "ok": ok, "reasons": reasons}
            report["rows"].append(entry)
            if ok:
                out_lines.append(line)
            else:
                report["held"].append(entry)
            continue
        out_lines.append(line)

    Path(args.out_queue).write_text("\n".join(out_lines) + "\n")
    Path(args.report).write_text(json.dumps(report, indent=2))
    held = len(report["held"])
    total = len(report["rows"])
    print(f"verify-queue: {total - held}/{total} rows verified at {now}; "
          f"{held} held", file=sys.stderr)
    for h in report["held"]:
        print(f"  HELD: {h['title']} -> {'; '.join(h['reasons'])}",
              file=sys.stderr)
    if "live_cards" in report and not report["live_cards"]["checked"]:
        print(f"verify-queue: phone question cards NOT checked: {report['live_cards']['error']}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
