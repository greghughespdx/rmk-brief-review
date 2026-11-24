"""mc-fezd regression: write-time verification must equal render-time parsing.

2026-08-14 06:00 the decisions push failed on a 21/21-verified queue because
verify-queue split cells its own way and validated only the Ask cell with a
placeholder Age. These tests pin the two escaped defects (phantom trailing
cell, unchecked Age format) and the contract that validate_row and
parse_review_queue agree on every row.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "remarkable" / "today_page.py"
# Reuse an already-loaded today_page instead of loading a second copy under
# the same sys.modules key. decision_resolver_v3 (imported by other test
# modules via plain `import today_page as tp`) binds its own `tp` reference
# once, at its first import; unconditionally overwriting sys.modules here
# left that reference pointing at a different TodayPageError class than the
# one this file's `today_page.TodayPageError` names, so assertRaises no
# longer caught the exception decision_resolver_v3 actually raised.
if "today_page" in sys.modules:
    today_page = sys.modules["today_page"]
else:
    SPEC = importlib.util.spec_from_file_location("today_page", MODULE_PATH)
    today_page = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = today_page
    SPEC.loader.exec_module(today_page)

GOOD_ASK = (
    "The widget needs a decision. "
    "Recommendation: Approve the widget because it is ready. "
    "Decide: (1:approve) [yes/no] Approve the widget? "
    "Row: widget_approve Read: tests/fixtures/remarkable/reading-sample.md"
)
GOOD_ROW = f"| Approve the widget? | example-project | 2026-08-14 | {GOOD_ASK} |"
# Identical row with ONE trailing space after the final pipe: the 2026-08-14
# phantom-cell defect. Ask-only validation passed it; the renderer rejected it.
PHANTOM_ROW = GOOD_ROW + " "
# Real Age string that escaped verification on 2026-08-14 (wiki row).
BAD_AGE_ROW = (
    "| Approve the widget? | example-project | "
    f"2026-08-03; scoped 2026-08-08 | {GOOD_ASK} |"
)


def queue_file(tmpdir: str, rows: list[str]) -> Path:
    path = Path(tmpdir) / "REVIEW-QUEUE.md"
    body = ["# Review Queue", "", "## Active", "",
            "| Item | Project | Age | Ask |", "|---|---|---|---|",
            *rows, "", "## Closed", "",
            "| Item | Closed | Disposition |", "|---|---|---|", ""]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


class ValidateRowMatchesRenderer(unittest.TestCase):
    def render_ok(self, row: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                parsed = today_page.parse_review_queue(queue_file(tmp, [row]))
            except today_page.TodayPageError:
                return False
            return len(parsed) == 1

    def test_good_row_passes_both(self):
        self.assertEqual(today_page.validate_row(GOOD_ROW), [])
        self.assertTrue(self.render_ok(GOOD_ROW))

    def test_phantom_trailing_cell_caught(self):
        violations = today_page.validate_row(PHANTOM_ROW)
        self.assertTrue(violations, "trailing-space phantom cell must be held")
        self.assertFalse(self.render_ok(PHANTOM_ROW))

    def test_bad_age_format_caught(self):
        violations = today_page.validate_row(BAD_AGE_ROW)
        self.assertTrue(any("age" in v.lower() for v in violations), violations)
        self.assertFalse(self.render_ok(BAD_AGE_ROW))

    def test_agreement_on_every_case(self):
        """validate_row says clean IFF the renderer accepts the row."""
        cases = [GOOD_ROW, PHANTOM_ROW, BAD_AGE_ROW,
                 "| too | few | cells |",
                 "| t | p | 2026-08-14 | No markers here. |"]
        for row in cases:
            clean = today_page.validate_row(row) == []
            self.assertEqual(clean, self.render_ok(row), row)


class TicketIdPatternCoversChildTickets(unittest.TestCase):
    """mc-9jd4t: the id pattern stopped at the dot, so a row citing a child
    ticket was checked against its parent instead. 42 dotted tickets exist."""

    def load_verify_queue(self):
        path = ROOT / "remarkable" / "verify-queue.py"
        spec = importlib.util.spec_from_file_location("verify_queue_mod", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dotted_child_id_is_matched_whole(self):
        pattern = self.load_verify_queue().MC_ID
        self.assertEqual(
            ["mc-1vmey.3", "mc-pwk7o.19"],
            pattern.findall("cites mc-1vmey.3 and mc-pwk7o.19 here"),
        )

    def test_plain_id_still_matches_and_keeps_no_suffix(self):
        pattern = self.load_verify_queue().MC_ID
        self.assertEqual(
            ["mc-4z8v", "mc-8d4te"],
            pattern.findall("cites mc-4z8v, and mc-8d4te."),
        )

    def test_row_citing_a_closed_child_ticket_is_held(self):
        module = self.load_verify_queue()
        ask = GOOD_ASK.replace(
            "The widget needs a decision.",
            "The widget needs a decision, per mc-pwk7o.19.",
        )
        row = f"| Approve the widget? | example-project | 2026-08-14 | {ask} |"
        cells = today_page._split_row(row)
        # The parent is open; only the child is closed. Before the fix the
        # pattern read "mc-pwk7o" and the row sailed through.
        tickets = {"mc-pwk7o": "open", "mc-pwk7o.19": "closed"}
        ok, reasons = module.check_row(row, cells, tickets)
        self.assertFalse(ok)
        self.assertTrue(
            any("mc-pwk7o.19" in reason and "closed" in reason
                for reason in reasons),
            reasons,
        )



class FactsFreshnessGateTests(unittest.TestCase):
    RENDER_DATE = "2026-10-07"
    FACTS_OK = (
        "Ticket: mc-rexce Facts: tests/fixtures/remarkable/facts-verifier.py "
        "tests/fixtures/remarkable/facts-ok.json "
    )
    FACTS_MISMATCH = (
        "Ticket: mc-rexce Facts: tests/fixtures/remarkable/facts-verifier.py "
        "tests/fixtures/remarkable/facts-mismatch.json "
    )

    def row_with(self, prefix, suffix=""):
        ask = (
            "Today the report still has 371 rows and 91 authority-backed rows. "
            "Recommendation: use the verified slice because the full report "
            "still has errors. Decide: (1:approve) [yes/no] Use it? "
            + suffix
            + "Row: widget_approve Read: tests/fixtures/remarkable/reading-sample.md"
        )
        return f"| {prefix} | example-project | 2026-10-07 | {ask} |"

    def run_verify_queue(self, queue):
        out_q = queue.parent / "verified.md"
        report_p = queue.parent / "report.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "remarkable" / "verify-queue.py"),
                "--queue",
                str(queue),
                "--out-queue",
                str(out_q),
                "--report",
                str(report_p),
                "--render-date",
                self.RENDER_DATE,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out_q, json.loads(report_p.read_text())

    def test_triggered_facts_need_a_facts_line(self):
        reasons = today_page.validate_row(
            self.row_with("Stale card"), render_date=self.RENDER_DATE
        )
        self.assertEqual(1, len(reasons))
        self.assertIn("render-time Facts check", reasons[0])

    def test_legacy_checked_date_is_ignored_because_the_verifier_runs(self):
        legacy = self.FACTS_OK + "checked 2026-10-06 "
        row = self.row_with("Legacy checked date", legacy)
        self.assertEqual([], today_page.validate_row(row, render_date=self.RENDER_DATE))
        parsed = today_page.parse_review_queue_text(
            "## Active\n" + row + "\n", render_date=self.RENDER_DATE
        )[0]
        self.assertEqual("2026-10-06", parsed["fact_check"]["legacy_checked_on"])
        self.assertEqual(0, parsed["fact_check"]["exit_code"])
        self.assertIn("verified_at", parsed["fact_check"])

    def test_count_claim_in_decision_question_requires_facts_line(self):
        ask = (
            "This card is timeless. Recommendation: approve the request. "
            "Decide: (1:approve) [yes/no] Do the 371 rows cover every event? "
            "Row: approve Read: tests/fixtures/remarkable/reading-sample.md"
        )
        row = f"| Question-only count | example-project | 2026-10-07 | {ask} |"
        reasons = today_page.validate_row(row, render_date=self.RENDER_DATE)
        self.assertEqual(1, len(reasons))
        self.assertIn("render-time Facts check", reasons[0])

    def test_choice_label_count_claim_requires_facts_line(self):
        ask = (
            "This card is timeless. Recommendation: choose A. "
            "Decide: (1:approve) [choice: A = ship all 371 rows, "
            "B = do not ship] Which path? "
            "Row: approve Read: tests/fixtures/remarkable/reading-sample.md"
        )
        row = f"| Choice-label count | example-project | 2026-10-07 | {ask} |"
        reasons = today_page.validate_row(row, render_date=self.RENDER_DATE)
        self.assertEqual(1, len(reasons))
        self.assertIn("render-time Facts check", reasons[0])

    def test_iso_date_claim_requires_facts_line(self):
        ask = (
            "This card says the outage started 2026-10-05. "
            "Recommendation: approve the summary. "
            "Decide: (1:approve) [yes/no] Approve it? "
            "Row: approve Read: tests/fixtures/remarkable/reading-sample.md"
        )
        row = f"| ISO date claim | example-project | 2026-10-07 | {ask} |"
        reasons = today_page.validate_row(row, render_date=self.RENDER_DATE)
        self.assertEqual(1, len(reasons))
        self.assertIn("render-time Facts check", reasons[0])

    def test_title_count_claim_requires_facts_line(self):
        ask = (
            "This card is timeless. Recommendation: approve the request. "
            "Decide: (1:approve) [yes/no] Approve it? "
            "Row: approve Read: tests/fixtures/remarkable/reading-sample.md"
        )
        row = f"| Report has 371 rows | example-project | 2026-10-07 | {ask} |"
        reasons = today_page.validate_row(row, render_date=self.RENDER_DATE)
        self.assertEqual(1, len(reasons))
        self.assertIn("render-time Facts check", reasons[0])

    def test_title_iso_date_claim_requires_facts_line(self):
        ask = (
            "This card is timeless. Recommendation: approve the request. "
            "Decide: (1:approve) [yes/no] Approve it? "
            "Row: approve Read: tests/fixtures/remarkable/reading-sample.md"
        )
        row = f"| Outage started 2026-10-05 | example-project | 2026-10-07 | {ask} |"
        reasons = today_page.validate_row(row, render_date=self.RENDER_DATE)
        self.assertEqual(1, len(reasons))
        self.assertIn("render-time Facts check", reasons[0])

    def test_fleet_report_style_count_mismatch_is_held(self):
        reasons = today_page.validate_row(
            self.row_with("Fleet report facts", self.FACTS_MISMATCH),
            render_date=self.RENDER_DATE,
        )
        self.assertEqual(1, len(reasons))
        self.assertIn("report counts differ", reasons[0])

    def test_current_legacy_checked_date_does_not_override_failing_verifier(self):
        current_but_failing = self.FACTS_MISMATCH + "checked 2026-10-07 "
        reasons = today_page.validate_row(
            self.row_with("Current date failing facts", current_but_failing),
            render_date=self.RENDER_DATE,
        )
        self.assertEqual(1, len(reasons))
        self.assertIn("report counts differ", reasons[0])

    def test_matching_facts_line_passes_and_preserves_ticket_marker_for_premise_watch(self):
        row = self.row_with("Fresh card", self.FACTS_OK)
        self.assertEqual([], today_page.validate_row(row, render_date=self.RENDER_DATE))
        parsed = today_page.parse_review_queue_text(
            "## Active\n" + row + "\n", render_date=self.RENDER_DATE
        )[0]
        self.assertEqual("mc-rexce", parsed["ticket_links"])
        self.assertEqual("widget_approve", parsed["row_key"])
        self.assertEqual(0, parsed["fact_check"]["exit_code"])
        self.assertIn("verified_at", parsed["fact_check"])

    def test_parked_row_is_checked_only_when_promoted_to_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            parked = Path(tmp) / "parked.md"
            row = self.row_with("After-trip card")
            parked.write_text(
                "# Queue\n\n## Waiting until the reader is back\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                + row
                + "\n",
                encoding="utf-8",
            )
            _out, report = self.run_verify_queue(parked)
            self.assertEqual([], report["rows"])

            active = Path(tmp) / "active.md"
            active.write_text(
                "# Queue\n\n## Active\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                + row
                + "\n",
                encoding="utf-8",
            )
            _out, report = self.run_verify_queue(active)
            self.assertEqual(1, len(report["held"]))
            self.assertIn("render-time Facts check", report["held"][0]["reasons"][0])

    def test_generate_passes_queue_path_to_facts_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            facts = (
                "Facts: tests/fixtures/remarkable/facts-verifier.py "
                "tests/fixtures/remarkable/facts-requires-queue.json "
            )
            queue = queue_file(tmp, [self.row_with("Queue required fact", facts)])
            verified, report = self.run_verify_queue(queue)
            self.assertEqual([], report["held"])
            report_p = base / "report.json"
            report_p.write_text(json.dumps(report), encoding="utf-8")
            state = base / "question-state.json"
            state.write_text('{"schema_version": 1, "entries": {}}', encoding="utf-8")
            out_dir = base / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "remarkable" / "today_page.py"),
                    "generate",
                    "--queue",
                    str(verified),
                    "--verify-report",
                    str(report_p),
                    "--out-dir",
                    str(out_dir),
                    "--date",
                    self.RENDER_DATE,
                    "--state",
                    str(state),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(
        os.environ.get("MC_LIVE_QUEUE_TESTS") == "1",
        "reads the real REVIEW-QUEUE.md and runs its Facts checks, which can run git fetch and ssh; "
        "set MC_LIVE_QUEUE_TESTS=1 to run it (mc-0mtb0, the cross-reviewer's review of c19bb9a2)",
    )
    def test_generate_renders_current_branch_queue_with_facts_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_rmapi = base / "rmapi"
            fake_rmapi.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = find ]; then exit 0; fi\n"
                "echo unexpected rmapi command >&2\n"
                "exit 2\n",
                encoding="utf-8",
            )
            fake_rmapi.chmod(0o755)
            queue = base / "REVIEW-QUEUE.md"
            queue.write_text(
                (ROOT / "REVIEW-QUEUE.md").read_text(encoding="utf-8").replace(
                    "Facts: facts/fleet_counts.py",
                    f"Facts: facts/fleet_counts.py --rmapi {fake_rmapi}",
                ),
                encoding="utf-8",
            )
            verified, report = self.run_verify_queue(queue)
            self.assertEqual(1, len(report["rows"]))
            self.assertEqual([], report["held"])
            self.assertEqual(base / "verified.md", verified)
            self.assertTrue((base / "report.json").exists())
            report_p = base / "report.json"
            report_p.write_text(json.dumps(report), encoding="utf-8")
            state = base / "question-state.json"
            state.write_text('{"schema_version": 1, "entries": {}}', encoding="utf-8")
            out_dir = base / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "remarkable" / "today_page.py"),
                    "generate",
                    "--queue",
                    str(verified),
                    "--verify-report",
                    str(report_p),
                    "--out-dir",
                    str(out_dir),
                    "--date",
                    self.RENDER_DATE,
                    "--state",
                    str(state),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            manifest = json.loads((out_dir / f"decisions-{self.RENDER_DATE}.manifest.json").read_text())
            self.assertEqual(1, len(manifest["cards"]))
            self.assertEqual([], manifest["held"])
            self.assertEqual(0, manifest["cards"][0]["fact_check"]["exit_code"])

    def test_real_generate_uses_filtered_queue_and_names_held_stale_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = queue_file(tmp, [self.row_with("Held stale card")])
            verified, report = self.run_verify_queue(queue)
            self.assertEqual(1, len(report["held"]))
            report_p = base / "report.json"
            report_p.write_text(json.dumps(report), encoding="utf-8")
            state = base / "question-state.json"
            state.write_text('{"schema_version": 1, "entries": {}}', encoding="utf-8")
            out_dir = base / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "remarkable" / "today_page.py"),
                    "generate",
                    "--queue",
                    str(verified),
                    "--verify-report",
                    str(report_p),
                    "--out-dir",
                    str(out_dir),
                    "--date",
                    self.RENDER_DATE,
                    "--state",
                    str(state),
                ],
                capture_output=True,
                text=True,
            )
            # Every row held: the manifest is written and no PDF is rendered,
            # which generate reports as EXIT_NO_CARDS (6), not as success.
            self.assertEqual(proc.returncode, 6, proc.stderr)
            manifest = json.loads((out_dir / f"decisions-{self.RENDER_DATE}.manifest.json").read_text())
            self.assertEqual([], manifest["cards"])
            self.assertEqual("Held stale card", manifest["held"][0]["title"])
            self.assertIn("render-time Facts check", manifest["held"][0]["reason"])

class VerifyQueueHoldsBadRows(unittest.TestCase):
    def test_verify_queue_holds_phantom_and_bad_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = queue_file(tmp, [GOOD_ROW, PHANTOM_ROW, BAD_AGE_ROW])
            out_q = Path(tmp) / "out.md"
            report_p = Path(tmp) / "report.json"
            proc = subprocess.run(
                [sys.executable,
                 str(ROOT / "remarkable" / "verify-queue.py"),
                 "--queue", str(queue), "--out-queue", str(out_q),
                 "--report", str(report_p)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(report_p.read_text())
            self.assertEqual(len(report["rows"]), 3)
            self.assertEqual(len(report["held"]), 2, report)
            # The filtered queue must render cleanly - the actual mc-fezd promise.
            parsed = today_page.parse_review_queue(out_q)
            self.assertEqual(len(parsed), 1)


if __name__ == "__main__":
    unittest.main()
