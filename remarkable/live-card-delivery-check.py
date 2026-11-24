#!/usr/bin/env python3
"""Delivery-time phone-card check for an already rendered Decisions document (mc-0mtb0).

The cross-reviewer's review of c19bb9a2: the 06:00 job delivers the document reviewed at 05:00 without
re-rendering it (push-decisions.sh --no-generate), so the render-time live-card gate in
verify-queue.py never sees a phone card opened or answered after that render. This check reads
the exact manifest being delivered, never re-renders and never writes to it, and asks the answer
service whether any question printed on it now has a phone card that is open, answered,
discarded, or waiting in review to go to the phone. If one does, the whole delivery is withheld:
the reviewed bytes stay as they are (no card or page is removed), and a fresh render and review
are needed. Deferred cards are reported, not withheld (where a deferred
question returns is decided on the service side).

  live-card-delivery-check.py --manifest FILE --live-cards URL --report FILE

Exit 0: deliver (no live card on a printed question, or the service could not be read; the
report says which). Exit 3: withhold, a live card. Exit 4: withhold, manifest error: the manifest fails
today_page.validate_manifest, the generator's own full check (entry ids present and unique, every card's
nonempty decision_entry_ids a subset of them, pages, and the manifest_hash integrity seal), decided before
any lookup. Exit 2: the manifest could not be read at all.
Token: env PHONE_CARDS_VERIFY_TOKEN, else the output of PHONE_CARDS_TOKEN_COMMAND; never printed.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def verify_queue():
    spec = importlib.util.spec_from_file_location("verify_queue", HERE / "verify-queue.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def manifest_problem(manifest):
    """None when the generator's own validator accepts the manifest, else its reason."""
    sys.path.insert(0, str(HERE))
    import today_page
    try:
        today_page.validate_manifest(manifest)
    except Exception as exc:  # TodayPageError, or a shape the validator could not even read
        return str(exc) or type(exc).__name__
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--live-cards", required=True, metavar="URL")
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        cards = manifest["cards"]
    except Exception as exc:
        print(f"live-card-delivery-check: manifest unreadable ({type(exc).__name__})", file=sys.stderr)
        return 2
    problem = manifest_problem(manifest)
    if problem:  # malformed metadata cannot show which questions are printed: withhold, never look up
        Path(args.report).write_text(json.dumps({"manifest": args.manifest, "checked": False,
                                                 "manifest_error": problem, "withhold": [], "deferred": []},
                                                indent=2))
        print(f"live-card-delivery-check: manifest error: {problem}; withholding", file=sys.stderr)
        return 4
    printed = [(c.get("headline", ""), c.get("row_key"), e) for c in cards for e in c["decision_entry_ids"]]
    vq = verify_queue()
    live, why = vq.fetch_live_cards(args.live_cards)
    report = {"manifest": args.manifest, "checked": live is not None, "error": why, "withhold": [], "deferred": []}
    for headline, row_key, qid in printed:
        found = [c for c in (live or {}).get(qid, []) if c.get("status") in vq.LIVE_PRIORITY]
        if not found:
            continue
        found.sort(key=lambda c: vq.LIVE_PRIORITY.index(c["status"]))
        hit = {"headline": headline, "row_key": row_key, "question_id": qid, "card_id": found[0].get("card_id"),
               "status": found[0]["status"]}
        report["withhold" if hit["status"] in vq.LIVE_HOLD else "deferred"].append(hit)
    Path(args.report).write_text(json.dumps(report, indent=2))
    if not report["checked"]:
        print(f"live-card-delivery-check: phone question cards NOT checked: {why}; delivering", file=sys.stderr)
        return 0
    if report["withhold"]:
        for h in report["withhold"]:
            print(f"  LIVE: {h['headline']} -> phone card {h['card_id']} is {h['status']}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
