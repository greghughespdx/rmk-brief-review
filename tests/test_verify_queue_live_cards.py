"""mc-0mtb0: the queue verifier holds a row whose question has a live phone card.

Design docs/designs/phone-question-cards-2026-11-07.md section 6 item 4. A row whose
question was answered or discarded on the phone, or is open there today, must
not print on the Decisions document. The answer service is a local fake here;
nothing reaches the live service or any secret store.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_remarkable_row_contract import GOOD_ROW, ROOT, queue_file, today_page  # noqa: E402

VERIFY = ROOT / "remarkable" / "verify-queue.py"
QID = today_page._entry_identity(today_page.row_item_id("widget_approve"), "approve")
TOKEN = "verifier-test-token"


class FakeService:
    def __init__(self):
        self.cards, self.requests = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("X-Ask-Verify-Token")))
                if self.path != "/v1/linked" or self.headers.get("X-Ask-Verify-Token") != TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({"cards": outer.cards}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # RMW_TEST_HTTP_PORT pins the port so an offline sandbox can allow just it.
        self.server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("RMW_TEST_HTTP_PORT", "0"))), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def card(self, status, qid=QID, kind=None, card_id="c0ffee000001"):
        self.cards.append({"question_id": qid, "card_id": card_id, "status": status,
                           "answer_kind": kind, "answered_at": None})


class LiveCardGate(unittest.TestCase):
    def setUp(self):
        self.svc = FakeService()
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = queue_file(self.tmp.name, [GOOD_ROW])

    def tearDown(self):
        self.svc.server.shutdown()
        self.svc.server.server_close()
        self.tmp.cleanup()

    def run_vq(self, live=True, env=None, url=None):
        out_q = Path(self.tmp.name) / "verified.md"
        report_p = Path(self.tmp.name) / "report.json"
        cmd = [sys.executable, str(VERIFY), "--queue", str(self.queue), "--out-queue", str(out_q),
               "--report", str(report_p), "--render-date", "2026-11-09"]
        if live:
            cmd += ["--live-cards", url or self.svc.url]
        e = {k: v for k, v in os.environ.items() if not k.startswith("PHONE_CARDS_")}
        e.update({"PHONE_CARDS_VERIFY_TOKEN": TOKEN, "PHONE_CARDS_TOKEN_COMMAND": "/usr/bin/false"})
        e.update(env or {})
        e = {k: v for k, v in e.items() if v is not None}
        proc = subprocess.run(cmd, capture_output=True, text=True, env=e)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out_q.read_text(), json.loads(report_p.read_text()), proc.stderr

    def assert_printed(self, verified, report):
        self.assertIn("Approve the widget?", verified)
        self.assertEqual([], report["held"])

    def assert_held(self, verified, report, reason):
        self.assertNotIn("Approve the widget?", verified)
        self.assertEqual(1, len(report["held"]))
        self.assertEqual([reason], report["held"][0]["reasons"])

    def test_control_no_live_card_prints_the_row(self):
        self.svc.card("answered", qid="0123456789ab", kind="yes")  # another question's card
        verified, report, _ = self.run_vq()
        self.assert_printed(verified, report)
        self.assertEqual(report["live_cards"], {"checked": True, "error": None, "held": [], "deferred": []})
        self.assertEqual(self.svc.requests, [("/v1/linked", TOKEN)])

    def test_answered_on_the_phone_holds_the_row(self):
        self.svc.card("answered", kind="no")
        verified, report, err = self.run_vq()
        self.assert_held(verified, report, "it was already answered on the phone, and the row has not been closed yet")
        self.assertEqual(report["live_cards"]["held"][0]["question_key"], "approve")
        self.assertEqual(report["live_cards"]["held"][0]["card_id"], "c0ffee000001")
        self.assertIn("HELD: Approve the widget?", err)

    def test_discarded_on_the_phone_holds_the_row(self):
        self.svc.card("discarded", kind="discard")
        verified, report, _ = self.run_vq()
        self.assert_held(verified, report, "it was already discarded on the phone, and the row has not been closed yet")

    def test_open_on_the_phone_holds_the_row(self):
        self.svc.card("open")
        verified, report, _ = self.run_vq()
        self.assert_held(verified, report, "it is open as a question on the phone today")

    def test_card_in_review_holds_the_row(self):
        """The cross-reviewer's cross-PR finding: with live review on, a phone card waits in 'review' for minutes before
        it is pushed, so the row must not print on the tablet the same morning."""
        self.svc.card("review")
        verified, report, _ = self.run_vq()
        self.assert_held(verified, report, "it is waiting for review before it goes to the phone today")
        self.assertEqual(report["live_cards"]["held"][0]["status"], "review")

    def test_answered_wins_over_an_older_expired_card(self):
        self.svc.card("expired", card_id="c0ffee000002")
        self.svc.card("answered", kind="yes")
        verified, report, _ = self.run_vq()
        self.assert_held(verified, report, "it was already answered on the phone, and the row has not been closed yet")

    def test_deferred_is_reported_not_held(self):
        self.svc.card("deferred", kind="defer")
        verified, report, _ = self.run_vq()
        self.assert_printed(verified, report)
        self.assertEqual([d["status"] for d in report["live_cards"]["deferred"]], ["deferred"])

    def test_expired_or_withdrawn_cards_do_not_hold(self):
        self.svc.card("expired", card_id="c0ffee000003")
        self.svc.card("withdrawn", card_id="c0ffee000004")
        verified, report, _ = self.run_vq()
        self.assert_printed(verified, report)
        self.assertEqual(report["live_cards"]["held"], [])

    def test_unreachable_service_holds_nothing_and_says_so(self):
        self.svc.card("answered", kind="yes")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = "http://127.0.0.1:%d" % s.getsockname()[1]
        s.close()  # nothing listens on this port now
        verified, report, err = self.run_vq(url=dead)
        self.assert_printed(verified, report)
        self.assertFalse(report["live_cards"]["checked"])
        self.assertIn("could not be read", report["live_cards"]["error"])
        self.assertIn("phone question cards NOT checked", err)

    def test_no_token_holds_nothing_and_says_so(self):
        self.svc.card("answered", kind="yes")
        verified, report, err = self.run_vq(env={"PHONE_CARDS_VERIFY_TOKEN": None})
        self.assert_printed(verified, report)
        self.assertEqual(report["live_cards"]["error"], "no verifier token (CalledProcessError)")
        self.assertEqual(self.svc.requests, [])
        self.assertNotIn(TOKEN, err)

    def test_wrong_token_is_refused_and_reported(self):
        self.svc.card("answered", kind="yes")
        verified, report, err = self.run_vq(env={"PHONE_CARDS_VERIFY_TOKEN": "wrong"})
        self.assert_printed(verified, report)
        self.assertFalse(report["live_cards"]["checked"])
        self.assertNotIn("wrong", err)

    def test_without_the_flag_the_service_is_never_asked(self):
        self.svc.card("answered", kind="yes")
        verified, report, _ = self.run_vq(live=False)
        self.assert_printed(verified, report)
        self.assertNotIn("live_cards", report)
        self.assertEqual(self.svc.requests, [])

    def test_page_one_states_the_hold_in_plain_words(self):
        self.svc.card("open")
        _, report, _ = self.run_vq()
        path = Path(self.tmp.name) / "report.json"
        held = today_page.load_verification_report(path)["held"]
        self.assertEqual(held, [{"title": "Approve the widget?",
                                 "reason": "it is open as a question on the phone today"}])



DATE = "2026-11-10"
TOOLS = ROOT / "remarkable"


class DeliveryGate(unittest.TestCase):
    """The cross-reviewer's review of c19bb9a2: the 06:00 delivery of the reviewed bytes (--no-generate) must check
    the exact manifest against live phone cards, and withhold without re-rendering."""

    def setUp(self):
        self.svc = FakeService()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.bin = base / "stack" / "remarkable"
        self.bin.mkdir(parents=True)
        for name in ("push-decisions.sh", "live-card-delivery-check.py", "verify-queue.py"):
            (self.bin / name).write_bytes((TOOLS / name).read_bytes())
        (self.bin / "today_page.py").symlink_to(TOOLS / "today_page.py")  # the check reuses its validator
        for shared in ("rmw-env.sh", "rmw_config.py", "hooks"):  # config loader and hook forwarders
            (self.bin / shared).symlink_to(TOOLS / shared)
        self.calls = base / "calls"
        for name, body in (("review-receipt.py", "import os,sys\nopen(os.environ['CALL_LOG'],'a').write('receipt\\n')\n"),):
            (self.bin / name).write_text(body)
        for name in ("replace-decisions-document.sh", "rotate-decisions.sh"):
            (self.bin / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "$CALL_LOG"\n')
            (self.bin / name).chmod(0o755)
        (self.bin / "fake-python").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "fake-python").chmod(0o755)
        # A real document from the real generator, so the manifest is one today_page itself accepts.
        self.archive = base / "archive"
        self.archive.mkdir()
        queue = queue_file(str(base), [GOOD_ROW])
        report = base / "render-verify.json"
        report.write_text(json.dumps({"verified_at": "2026-11-10T05:00:00-08:00",
                                      "rows": [{"title": "Approve the widget?", "ok": True, "reasons": []}]}))
        today_page.generate(queue, None, report, self.archive / DATE, DATE, answer_record_root=self.archive,
                            state_path=base / "question-state.json")
        self.manifest_path = self.archive / DATE / f"decisions-{DATE}.manifest.json"
        self.original_manifest = self.manifest_path.read_bytes()
        assert [c["decision_entry_ids"] for c in json.loads(self.original_manifest)["cards"]] == [[QID]]
        self.before = {p.name: p.read_bytes() for p in (self.archive / DATE).iterdir()}

    def tearDown(self):
        self.svc.server.shutdown()
        self.svc.server.server_close()
        self.tmp.cleanup()

    def push(self, url=None, extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHONE_CARDS_")}
        env.update(CALL_LOG=str(self.calls), REMARKABLE_TODAY_ARCHIVE=str(self.archive),
                   REMARKABLE_TODAY_REGISTRY=str(Path(self.tmp.name) / "registry.json"),
                   TODAY_EXTRACTOR_PYTHON=str(self.bin / "fake-python"), PHONE_CARDS_URL=url or self.svc.url,
                   PHONE_CARDS_VERIFY_TOKEN=TOKEN, PHONE_CARDS_TOKEN_COMMAND="/usr/bin/false", **(extra or {}))
        p = subprocess.run(["bash", str(self.bin / "push-decisions.sh"), "--date", DATE, "--no-generate", "--push"],
                           env=env, capture_output=True, text=True, timeout=60)
        calls = self.calls.read_text() if self.calls.exists() else ""
        report_p = self.archive / f"live-cards-decisions-{DATE}.json"
        report = json.loads(report_p.read_text()) if report_p.exists() else None
        return p, calls, report

    def unchanged(self):
        day = self.archive / DATE
        return self.before == {p.name: p.read_bytes() for p in day.iterdir() if p.name in self.before}

    def test_reviewed_delivery_is_withheld_when_a_printed_question_is_answered_on_the_phone(self):
        self.svc.card("answered", kind="yes")
        p, calls, report = self.push()
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertIn("LIVE-CARD GATE", p.stderr)
        self.assertNotIn("replace-decisions-document.sh", calls)  # nothing delivered
        self.assertEqual([h["status"] for h in report["withhold"]], ["answered"])
        self.assertTrue(self.unchanged())  # the reviewed bytes are left as they are

    def test_card_in_review_also_withholds_the_whole_reviewed_document(self):
        self.svc.card("review")
        p, calls, report = self.push()
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertNotIn("replace-decisions-document.sh", calls)
        self.assertEqual([h["status"] for h in report["withhold"]], ["review"])
        self.assertTrue(self.unchanged())  # no card or page is removed from the reviewed PDF

    def test_open_on_the_phone_also_withholds(self):
        self.svc.card("open")
        self.assertEqual(self.push()[0].returncode, 7)

    def test_control_no_live_card_delivers_the_reviewed_bytes(self):
        self.svc.card("answered", qid="0123456789ab", kind="yes")  # another question's card
        p, calls, report = self.push()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("replace-decisions-document.sh", calls)
        self.assertEqual((report["checked"], report["withhold"]), (True, []))
        self.assertTrue(self.unchanged())

    def test_deferred_card_is_reported_and_delivered(self):
        self.svc.card("deferred", kind="defer")
        p, calls, report = self.push()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([h["status"] for h in report["deferred"]], ["deferred"])

    def test_unreadable_service_delivers_and_says_the_check_did_not_run(self):
        self.svc.card("answered", kind="yes")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = "http://127.0.0.1:%d" % s.getsockname()[1]
        s.close()
        p, calls, report = self.push(url=dead)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(report["checked"])
        self.assertIn("NOT checked", p.stderr)

    def rewrite_card(self, change, rehash=True):
        """Change only the card's metadata; by default re-seal manifest_hash so the integrity check passes
        and the structural rule itself is what is tested."""
        m = json.loads(self.original_manifest)
        change(m["cards"][0])
        if rehash:
            m["manifest_hash"] = today_page.manifest_digest(m)
        self.manifest_path.write_text(json.dumps(m))
        self.before = {p.name: p.read_bytes() for p in (self.archive / DATE).iterdir()}

    def test_card_without_entry_ids_is_a_manifest_error_that_withholds(self):
        """The cross-reviewer's preliminary review of 22826319: only the decision_entry_ids metadata is removed; the
        printed question's phone card is answered. That is malformed metadata, not 'nothing printed'."""
        for change in (lambda c: c.pop("decision_entry_ids"), lambda c: c.update(decision_entry_ids=[]),
                       lambda c: c.update(decision_entry_ids="not-a-list"), lambda c: c.update(decision_entry_ids=[""])):
            self.svc.card("answered", kind="yes")
            self.rewrite_card(change)
            self.svc.requests.clear()
            p, calls, report = self.push()
            self.assertEqual(p.returncode, 7, p.stderr)
            self.assertIn("manifest", p.stderr)
            self.assertNotIn("replace-decisions-document.sh", calls)
            self.assertTrue(report["manifest_error"])
            self.assertFalse(report["checked"])
            self.assertEqual(self.svc.requests, [])  # decided before any lookup
            self.assertTrue(self.unchanged())
            self.setUp_manifest()

    def setUp_manifest(self):
        self.manifest_path.write_bytes(self.original_manifest)
        self.before = {p.name: p.read_bytes() for p in (self.archive / DATE).iterdir()}

    def test_card_pointing_at_a_foreign_entry_id_withholds(self):
        """The cross-reviewer's round 3 probe: the card's ids are well formed but name no manifest entry, while the
        printed entry's phone card is answered. Not a valid manifest: withheld before any lookup."""
        self.svc.card("answered", kind="yes")
        self.rewrite_card(lambda c: c.update(decision_entry_ids=["abcdef012345"]))
        p, calls, report = self.push()
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertNotIn("replace-decisions-document.sh", calls)
        self.assertIn("Manifest card is invalid", report["manifest_error"])
        self.assertEqual(self.svc.requests, [])
        self.assertTrue(self.unchanged())

    def test_blank_or_non_string_ids_are_refused_even_when_consistent_and_resealed(self):
        """The cross-reviewer's preliminary review of 4303228e: an entry id of "" or an integer, with the card's mapping
        changed to the same value and the manifest re-sealed, was accepted by validate_manifest."""
        for bad in ("", "   ", 7):
            self.svc.card("answered", kind="yes")
            m = json.loads(self.original_manifest)
            m["entries"][0]["entry_id"] = bad
            m["cards"][0]["decision_entry_ids"] = [bad]
            m["manifest_hash"] = today_page.manifest_digest(m)
            self.manifest_path.write_text(json.dumps(m))
            self.before = {p.name: p.read_bytes() for p in (self.archive / DATE).iterdir()}
            self.svc.requests.clear()
            p, calls, report = self.push()
            self.assertEqual(p.returncode, 7, (bad, p.stderr))
            self.assertNotIn("replace-decisions-document.sh", calls)
            self.assertIn("entry ids", report["manifest_error"])
            self.assertEqual(self.svc.requests, [])
            self.assertTrue(self.unchanged())
            with self.assertRaises(today_page.TodayPageError):
                today_page.validate_manifest(m)
            self.setUp_manifest()

    def test_tampered_manifest_fails_its_integrity_check(self):
        self.svc.card("answered", kind="yes")
        self.rewrite_card(lambda c: c.update(headline="Edited after review"), rehash=False)
        p, calls, report = self.push()
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertIn("integrity", report["manifest_error"])
        self.assertEqual(self.svc.requests, [])

    def test_manifest_error_exit_code_is_distinct(self):
        self.rewrite_card(lambda c: c.pop("decision_entry_ids"))
        report = Path(self.tmp.name) / "r.json"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHONE_CARDS_")}
        env.update(PHONE_CARDS_VERIFY_TOKEN=TOKEN, PHONE_CARDS_TOKEN_COMMAND="/usr/bin/false")
        p = subprocess.run([sys.executable, str(self.bin / "live-card-delivery-check.py"), "--manifest",
                            str(self.archive / DATE / f"decisions-{DATE}.manifest.json"), "--live-cards", self.svc.url,
                            "--report", str(report)], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 4)  # not 0 (deliver) and not 3 (live card)

    def test_missing_check_script_refuses_to_push(self):
        (self.bin / "live-card-delivery-check.py").unlink()
        p, calls, _ = self.push()
        self.assertEqual(p.returncode, 7)
        self.assertNotIn("replace-decisions-document.sh", calls)


class MorningDashboardReviewedPath(unittest.TestCase):
    """The real run_decisions function from morning-dashboard.sh, reviewed-document branch, with only its
    outbound and logging boundaries replaced (the cross-reviewer's mc_gate_probes.py shape)."""

    def run_branch(self, push_rc, report=None):
        source = (TOOLS / "morning-dashboard.sh").read_text()
        start = source.index("live_push_note() {")
        end = source.index("\n}\nrun_decisions", start) + 2
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            day = tmp / "archive" / DATE
            day.mkdir(parents=True)
            for name in ("review-receipt.json", f"decisions-{DATE}.pdf", f"decisions-{DATE}.manifest.json"):
                (day / name).write_text("reviewed bytes")
            if report is not None:
                (tmp / "archive" / f"live-cards-decisions-{DATE}.json").write_text(json.dumps(report))
            push = tmp / "push"
            push.write_text(f'#!/bin/sh\necho "$*" >> "$PROBE_CALLS"\nexit {push_rc}\n')
            push.chmod(0o700)
            shell = tmp / "probe.sh"
            shell.write_text('note() { :; }\nbump() { :; }\nbus_note() { printf "%s|%s\\n" "$1" "$2" >> "$PROBE_BUS"; }\n'
                             'outbound() { shift; "$@"; }\nreport_held_decisions() { :; }\n'
                             + source[start:end] + '\nrun_decisions\necho "PHRASE=$DEC_PHRASE"\n')
            env = dict(os.environ, ARCHIVE_ROOT=str(tmp / "archive"), TODAY=DATE, PUSH_DECISIONS=str(push),
                       VERIFY_QUEUE="/usr/bin/false", TMP=str(tmp), PROBE_CALLS=str(tmp / "push.calls"),
                       PROBE_BUS=str(tmp / "bus"), HARD="2", SUPPRESSED="9")
            p = subprocess.run(["bash", str(shell)], env=env, capture_output=True, text=True, timeout=30)
            bus = (tmp / "bus").read_text() if (tmp / "bus").exists() else ""
            return p.stdout, bus, (tmp / "push.calls").read_text()

    def test_withheld_reviewed_delivery_is_reported_to_operators(self):
        out, bus, calls = self.run_branch(7, {"checked": True, "withhold": [{
            "headline": "Approve the widget?", "row_key": "widget_approve", "card_id": "c0ffee000001",
            "status": "answered"}]})
        self.assertIn("--no-generate", calls)
        self.assertIn("PHRASE=decisions withheld (a question on it is already on your phone)", out)
        self.assertIn("WITHHELD", bus)
        self.assertIn("Approve the widget?", bus)

    def test_manifest_error_is_named_in_the_note(self):
        _, bus, _ = self.run_branch(7, {"checked": False, "withhold": [], "deferred": [],
                                        "manifest_error": "card 1: decision_entry_ids is missing, empty or not a list of ids"})
        self.assertIn("manifest is malformed", bus)

    def test_reviewed_delivery_without_the_check_is_reported(self):
        out, bus, _ = self.run_branch(0, {"checked": False, "error": "no verifier token", "withhold": []})
        self.assertIn("PHRASE=decisions delivered", out)
        self.assertIn("not checked at delivery", bus)

    def test_control_checked_delivery_sends_no_note(self):
        out, bus, _ = self.run_branch(0, {"checked": True, "withhold": [], "deferred": []})
        self.assertIn("PHRASE=decisions delivered", out)
        self.assertEqual(bus, "")


if __name__ == "__main__":
    unittest.main()
