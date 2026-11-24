import copy
import datetime as dt_module
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADJACENT_BOX_TICKS = json.loads(
    (
        ROOT
        / "tests"
        / "fixtures"
        / "remarkable"
        / "decisions-2026-08-16-adjacent-box-ticks.json"
    ).read_text()
)
MODULE_PATH = ROOT / "remarkable" / "today_page.py"
if "today_page" in sys.modules:
    today_page = sys.modules["today_page"]
else:
    SPEC = importlib.util.spec_from_file_location("today_page", MODULE_PATH)
    today_page = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = today_page
    SPEC.loader.exec_module(today_page)

WATCH_PATH = ROOT / "remarkable" / "today_watch.py"
WATCH_SPEC = importlib.util.spec_from_file_location("today_watch", WATCH_PATH)
today_watch = importlib.util.module_from_spec(WATCH_SPEC)
sys.modules[WATCH_SPEC.name] = today_watch
WATCH_SPEC.loader.exec_module(today_watch)

try:
    import rmscene as _rmscene_probe  # noqa: F401

    HAVE_RMSCENE = True
except ImportError:
    HAVE_RMSCENE = False


class BoxBounds(list):
    def __init__(self, bounds, page):
        super().__init__(bounds)
        self.page = page


def stroke_for(bounds, stroke_id="tick", inset=2, page=None):
    x0, y0, x1, y1 = bounds
    return {
        "stroke_id": stroke_id,
        "page": page if page is not None else getattr(bounds, "page", 1),
        "points": [
            [x0 + inset, y0 + inset],
            [(x0 + x1) / 2, y1 - inset],
            [x1 - inset, y0 + inset],
        ],
    }


def extract_with_fake_rmscene(
    manifest,
    content,
    page_lines,
    paper_sizes=None,
    extra_rm_payloads=None,
):
    class FakePoint:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    class FakeLine:
        def __init__(self, points):
            self.points = [FakePoint(x, y) for x, y in points]

    class FakeTree:
        def __init__(self, payload):
            self.scene_info = types.SimpleNamespace(
                paper_size=tuple(payload["paper_size"])
            )
            self.lines = [FakeLine(points) for points in payload["lines"]]

        def walk(self):
            return self.lines

    rmscene = types.ModuleType("rmscene")
    rmscene.read_tree = lambda raw: FakeTree(json.loads(raw.read()))
    scene_items = types.ModuleType("rmscene.scene_items")
    scene_items.Line = FakeLine
    prior = {
        name: sys.modules.get(name)
        for name in ("rmscene", "rmscene.scene_items")
    }
    sys.modules["rmscene"] = rmscene
    sys.modules["rmscene.scene_items"] = scene_items
    try:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.rmdoc"
            source = f"{manifest['page_id']} {manifest['manifest_hash']}".encode()
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("source.pdf", source)
                bundle.writestr("document.content", json.dumps(content))
                for page_id, lines in page_lines.items():
                    paper_size = (paper_sizes or {}).get(page_id, (1404, 1872))
                    bundle.writestr(
                        f"nested/{page_id}.rm",
                        json.dumps(
                            {
                                "paper_size": list(paper_size),
                                "lines": lines,
                            }
                        ),
                    )
                for name, payload in (extra_rm_payloads or {}).items():
                    bundle.writestr(name, json.dumps(payload))
            return today_page.extract_v6(manifest, archive)
    finally:
        for name, value in prior.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class TodayPageTests(unittest.TestCase):
    def setUp(self):
        self.actions = [
            {
                "type": "ack_item",
                "label": "Mark summary read",
                "payload": {"item_id": "summary-1"},
            }
        ]
        self.decisions = [
            {
                "item_id": "decision-1",
                "title": "Choose the sample",
                "project": "test",
                "age": "2026-07-24; rev 2026-07-25",
                "ask": (
                    "This is the bounded sample. It is ready for a choice. "
                    "Recommendation: approve it. "
                    "Decide: (1:q1) [yes/no] Approve the bounded sample? Row: row_1 Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            },
            {
                "item_id": "decision-2",
                "title": "Choose another sample",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "This is the second bounded sample. It is ready for a choice. "
                    "Recommendation: approve it. "
                    "Decide: (1:q1) [yes/no] Approve the second bounded sample? Row: row_2 Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            },
        ]
        self.manifest = today_page.build_manifest(
            "2026-07-25", self.actions, self.decisions, {"review_queue": "abc"}
        )

    def strokes_doc(self, strokes):
        normalized = []
        for stroke in strokes:
            item = {**stroke, "page": stroke.get("page", 1)}
            if (
                item["page"] == 1
                and item.get("points")
                and all(
                    isinstance(coordinate, (int, float))
                    and not isinstance(coordinate, bool)
                    for point in item["points"]
                    for coordinate in point
                )
            ):
                x_values = [point[0] for point in item["points"]]
                y_values = [point[1] for point in item["points"]]
                center_x = (min(x_values) + max(x_values)) / 2
                center_y = (min(y_values) + max(y_values)) / 2
                matching_pages = {
                    entry["page"]
                    for entry in self.manifest["entries"]
                    for box in entry["boxes"]
                    if today_page._inside(center_x, center_y, box["bbox"])
                }
                if len(matching_pages) == 1:
                    item["page"] = matching_pages.pop()
            normalized.append(item)
        return {
            "schema_version": self.manifest["schema_version"],
            "layout_version": self.manifest["layout_version"],
            "page_id": self.manifest["page_id"],
            "strokes": normalized,
        }

    def entry_id_for(self, name):
        """Accept a real entry id or the positional name these tests were
        written with. Entry ids are content-derived now (sha256 of the row key
        and the question key), so "decision-001a" is read as "the first
        question of the first card" rather than as a literal id."""
        if name.startswith("decision-"):
            card_number = int(name[len("decision-"):-1])
            question = ord(name[-1]) - ord("a")
            card = self.manifest["cards"][card_number - 1]
            return card["decision_entry_ids"][question]
        return name

    def find_box(self, entry_id, choice):
        entry_id = self.entry_id_for(entry_id)
        entry = next(item for item in self.manifest["entries"] if item["entry_id"] == entry_id)
        bounds = next(box for box in entry["boxes"] if box["choice"] == choice)["bbox"]
        return BoxBounds(bounds, entry["page"])

    def decision_entry(self, card_index=0, question_index=0):
        card = self.manifest["cards"][card_index]
        entry_id = card["decision_entry_ids"][question_index]
        return next(
            item for item in self.manifest["entries"] if item["entry_id"] == entry_id
        )

    def find_decision_box(self, choice, card_index=0, question_index=0):
        entry = self.decision_entry(card_index, question_index)
        bounds = next(box for box in entry["boxes"] if box["choice"] == choice)["bbox"]
        return BoxBounds(bounds, entry["page"])

    def adjacent_box_fixture_manifest(self, fixture):
        decision = {
            "item_id": "real-tick-regression",
            "title": "Choose the sample",
            "project": "test",
            "age": "2026-08-16",
            "ask": (
                "This fixture reproduces real pen geometry. It needs two choices. "
                "Recommendation: use the observed marks. "
                "Decide: (1:q1) [yes/no] First choice? Recommended: yes because the first one is ready. (2:q2) [yes/no] Adjacent choice? Recommended: yes because the second one is ready too. Row: row_3 Read: tests/fixtures/remarkable/reading-sample.md"
            ),
        }
        manifest = today_page.build_manifest(
            "2026-08-16", [], [decision], {"fixture": "real-bboxes"}
        )
        # The fixture carries the real 2026-08-16 YES/NO geometry. Keep each
        # entry's own Defer and Discard boxes behind it: v3 requires them on
        # every question, and dropping them would make this a manifest the
        # generator could never have written.
        for index, key in enumerate(("intended_boxes", "adjacent_boxes")):
            entry = manifest["entries"][index]
            tail = [
                box
                for box in entry["boxes"]
                if box["choice"] in ("DEFER", "DISCARD")
            ]
            entry["boxes"] = copy.deepcopy(fixture[key]) + tail
        manifest["manifest_hash"] = today_page.manifest_digest(manifest)
        return manifest

    @staticmethod
    def fixture_strokes(manifest, stroke_id, stroke_bbox):
        return {
            "schema_version": manifest["schema_version"],
            "layout_version": manifest["layout_version"],
            "page_id": manifest["page_id"],
            "strokes": [
                {
                    "stroke_id": stroke_id,
                    "page": manifest["entries"][0]["page"],
                    "points": [stroke_bbox[:2], stroke_bbox[2:]],
                }
            ],
        }

    def test_every_allowed_action_validates(self):
        samples = {
            "close_ticket": {"ticket_id": "mc-1", "note": "Done"},
            "approve_queue_row": {"item_id": "row-1"},
            "ack_item": {"item_id": "item-1"},
            "defer_item": {"item_id": "item-1", "days": 7},
            "run_job": {"job_id": "scan"},
        }
        for action_type, payload in samples.items():
            with self.subTest(action_type=action_type):
                result = today_page.validate_action(
                    {"type": action_type, "label": "Safe action", "payload": payload}
                )
                self.assertEqual(action_type, result["type"])

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(today_page.TodayPageError):
            today_page.validate_action(
                {"type": "send_email", "label": "Do it", "payload": {"to": "somebody"}}
            )

    def test_extra_payload_key_is_rejected(self):
        with self.assertRaises(today_page.TodayPageError):
            today_page.validate_action(
                {
                    "type": "run_job",
                    "label": "Run it",
                    "payload": {"job_id": "safe", "command": "unsafe"},
                }
            )

    def test_defer_range_is_bounded(self):
        for days in (0, 31, "7"):
            with self.subTest(days=days), self.assertRaises(today_page.TodayPageError):
                today_page.validate_action(
                    {
                        "type": "defer_item",
                        "label": "Defer",
                        "payload": {"item_id": "x", "days": days},
                    }
                )

    def test_action_cap_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.json"
            path.write_text(json.dumps(self.actions * 6))
            with self.assertRaises(today_page.TodayPageError):
                today_page.load_actions(path)

    def test_review_queue_reads_all_active_sections_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "REVIEW-QUEUE.md"
            path.write_text(
                "# Queue\n\n"
                "## Active - First\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                "| [One](one.md) | A | 2026-07-24 | One choice is ready. "
                "It needs an answer. Recommendation: choose one. "
                "Decide: (1:q1) [yes/no] Choose one? Row: row_4 Read: tests/fixtures/remarkable/reading-sample.md |\n\n"
                "## Active - Second\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                "| Two | B | 2026-07-25 | A second choice is ready. "
                "It needs an answer. Recommendation: choose two. "
                "Decide: (1:q1) [yes/no] Choose two? Row: row_5 Read: tests/fixtures/remarkable/reading-sample.md |\n\n"
                "## Closed\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                "| Old | C | yesterday | Already done. |\n"
            )
            rows = today_page.parse_review_queue(path)
        self.assertEqual(["One", "Two"], [row["title"] for row in rows])

    def test_queue_row_becomes_full_card_copy(self):
        decision = today_page._decision_copy(
            {
                "item_id": "one",
                "title": "Approve the useful change (design)",
                "project": "test",
                "age": "2026-07-24; rev 2026-07-25",
                "ask": (
                    "The useful change is ready. It has passed review. "
                    "Recommendation: approve it. "
                    "Decide: (1:q1) [yes/no] Approve the useful change? "
                    "Recommended: yes because it has passed review. "
                    "(2:q2) [yes/no] Publish it after approval? "
                    "Recommended: yes because nothing else waits on it. Row: row_6 Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
        )
        self.assertEqual("Approve the useful change?", decision["question"])
        self.assertEqual(
            "The useful change is ready. It has passed review.",
            decision["description"],
        )
        self.assertEqual("approve it.", decision["recommendation"])
        self.assertEqual(
            ["Approve the useful change?", "Publish it after approval?"],
            decision["decision_questions"],
        )
        self.assertEqual("Raised July 24. Revised July 25.", decision["dates"])

    # mc-9jd4t: on 2026-09-12 the reader edited and published the build-tool pull
    # request texts himself; the next morning the document still asked him to
    # approve them, because nothing in the render path looked at GitHub. A
    # card about a pull request or an issue now has to carry the live state
    # from the day it renders.
    GH_BASE = {
        "item_id": "gh",
        "title": "Keep the two extras in the pull request",
        "project": "test",
        "age": "2026-09-11",
    }
    GH_TAIL = " Row: row_gh Read: tests/fixtures/remarkable/reading-sample.md"
    GH_CORE = (
        "The pull request is open on the author's project. Two extras sit in "
        "it. Recommendation: remove them. "
        "Decide: (1:q1) [yes/no] Remove the two extras?"
    )

    def test_github_card_without_a_live_check_is_refused(self):
        with self.assertRaises(today_page.TodayPageError) as caught:
            today_page._decision_copy(
                {**self.GH_BASE, "ask": self.GH_CORE + self.GH_TAIL},
                render_date="2026-09-13",
            )
        self.assertIn("GH-checked", str(caught.exception))

    def test_github_card_with_a_stale_check_is_refused(self):
        ask = (
            self.GH_CORE
            + " GH-checked: owner/repo#74 OPEN updated "
            "2026-09-12T18:47:44Z, checked 2026-09-12."
            + self.GH_TAIL
        )
        with self.assertRaises(today_page.TodayPageError) as caught:
            today_page._decision_copy(
                {**self.GH_BASE, "ask": ask}, render_date="2026-09-13"
            )
        self.assertIn("2026-09-13", str(caught.exception))

    def test_github_card_with_a_same_day_check_renders(self):
        ask = (
            self.GH_CORE
            + " GH-checked: owner/repo#74 OPEN updated "
            "2026-09-12T18:47:44Z, checked 2026-09-13."
            + self.GH_TAIL
        )
        decision = today_page._decision_copy(
            {**self.GH_BASE, "ask": ask}, render_date="2026-09-13"
        )
        self.assertEqual(
            [
                {
                    "repo": "owner/repo",
                    "number": "74",
                    "state": "OPEN",
                    "updated_at": "2026-09-12T18:47:44Z",
                    "checked_on": "2026-09-13",
                }
            ],
            decision["gh_checks"],
        )
        self.assertNotIn("GH-checked", decision["description"])
        self.assertNotIn("GH-checked", decision["recommendation"])

    def test_github_card_may_record_that_no_pull_request_exists(self):
        ask = (
            self.GH_CORE
            + " GH-checked: owner/repo NONE checked 2026-09-13."
            + self.GH_TAIL
        )
        decision = today_page._decision_copy(
            {**self.GH_BASE, "ask": ask}, render_date="2026-09-13"
        )
        self.assertEqual("NONE", decision["gh_checks"][0]["state"])

    def test_github_check_must_be_well_formed(self):
        ask = (
            self.GH_CORE
            + " GH-checked: owner/repo#74 is still open."
            + self.GH_TAIL
        )
        with self.assertRaises(today_page.TodayPageError):
            today_page._decision_copy(
                {**self.GH_BASE, "ask": ask}, render_date="2026-09-13"
            )

    def test_card_without_a_github_reference_needs_no_check(self):
        decision = today_page._decision_copy(
            {
                **self.GH_BASE,
                "title": "Clear the two caches",
                "ask": (
                    "The disk is close to its floor. Two caches can be "
                    "refetched. Recommendation: clear both. "
                    "Decide: (1:q1) [yes/no] Clear both caches?"
                    + self.GH_TAIL
                ),
            },
            render_date="2026-09-13",
        )
        self.assertEqual([], decision["gh_checks"])

    def test_validate_row_holds_a_github_row_with_no_live_check(self):
        line = (
            "| Keep the two extras in the pull request | test | 2026-09-11 | "
            + self.GH_CORE
            + self.GH_TAIL
            + " |"
        )
        reasons = today_page.validate_row(line, render_date="2026-09-13")
        self.assertTrue(reasons)
        self.assertIn("GH-checked", reasons[0])

    # mc-9jd4t: the durable answer record covered pen marks only. An answer
    # The reader gave out loud or by iMessage was written into the row as prose,
    # which nothing machine-readable ever read, so the question came back.
    ANSWERED_BASE = {
        "item_id": "ans",
        "title": "Publish the assessment",
        "project": "test",
        "age": "2026-09-11",
    }

    def test_row_printing_a_question_answered_in_session_is_refused(self):
        ask = (
            "The assessment could go out. Recommendation: send it. "
            "Decide: (1:q1) [yes/no] Send the assessment? "
            "Answered: q1 on 2026-09-13 by session."
            + self.GH_TAIL
        )
        with self.assertRaises(today_page.TodayPageError) as caught:
            today_page._decision_copy(
                {**self.ANSWERED_BASE, "ask": ask}, render_date="2026-09-13"
            )
        message = str(caught.exception)
        self.assertIn("q1", message)
        self.assertIn("session", message)

    def test_row_printing_a_question_answered_by_imessage_is_refused(self):
        ask = (
            "The assessment could go out. Recommendation: send it. "
            "Decide: (1:q1) [yes/no] Send the assessment? "
            "Answered: q1 on 2026-09-12 by imessage."
            + self.GH_TAIL
        )
        with self.assertRaises(today_page.TodayPageError):
            today_page._decision_copy(
                {**self.ANSWERED_BASE, "ask": ask}, render_date="2026-09-13"
            )

    def test_recorded_answer_for_a_removed_question_lets_the_row_print(self):
        ask = (
            "One question was settled in session; this one is open. "
            "Recommendation: send it. "
            "Decide: (1:q2) [yes/no] Send the assessment? "
            "Answered: q1 on 2026-09-13 by session."
            + self.GH_TAIL
        )
        decision = today_page._decision_copy(
            {**self.ANSWERED_BASE, "ask": ask}, render_date="2026-09-13"
        )
        self.assertEqual(["q2"], [f["question_key"] for f in decision["decision_forms"]])
        self.assertEqual(
            {"q1": {"answered_on": "2026-09-13", "channel": "session"}},
            decision["answered_on_row"],
        )
        self.assertNotIn("Answered:", decision["description"])

    def test_recorded_answer_must_be_well_formed(self):
        ask = (
            "The assessment could go out. Recommendation: send it. "
            "Decide: (1:q2) [yes/no] Send the assessment? "
            "Answered: he said yes yesterday."
            + self.GH_TAIL
        )
        with self.assertRaises(today_page.TodayPageError):
            today_page._decision_copy(
                {**self.ANSWERED_BASE, "ask": ask}, render_date="2026-09-13"
            )

    # mc-9jd4t: the cover named each held row with its whole title, which is
    # free prose no freshness check ever reads. On 2026-09-13 that printed
    # "three builds and their manifests remain on the Mac Studio" on the cover
    # while the card three pages later said eight archives and 11 GB.
    def test_cover_names_a_held_row_by_subject_not_by_its_whole_title(self):
        title = (
            "build-tool build archive policy: 25 older build files (12 archives "
            "and 13 manifests, 16.8 GB) moved to the NAS overnight under the "
            "move-not-delete rule; three builds and their manifests remain on "
            "the Mac Studio"
        )
        label = today_page._cover_held_label(title)
        self.assertEqual("build-tool build archive policy", label)
        for stale in ("three builds", "16.8 GB", "12 archives"):
            self.assertNotIn(stale, label)

    def test_cover_held_label_drops_a_trailing_parenthetical(self):
        self.assertEqual(
            "Scheduled jobs off the shared checkout",
            today_page._cover_held_label(
                "Scheduled jobs off the shared checkout: proposal ready, "
                "direction needed (docs/designs/x.md, mc-yp8d7)"
            ),
        )

    def test_cover_held_label_caps_a_long_title_with_no_colon(self):
        label = today_page._cover_held_label(
            "one two three four five six seven eight nine ten eleven twelve "
            "thirteen fourteen"
        )
        self.assertEqual(today_page.COVER_HELD_LABEL_WORDS, len(label.split()))

    def test_cover_prints_the_short_label_and_the_reason(self):
        blocks = today_page._cover_blocks(
            [
                {
                    "title": (
                        "build-tool build archive policy: three builds and their "
                        "manifests remain on the Mac Studio"
                    ),
                    "reason": "card contract: no Decide section",
                }
            ],
            card_count=18,
            inner_width=552.0,
        )
        printed = " ".join(
            line
            for block in blocks
            if block["kind"] == "cover_item"
            for line in block["lines"]
        )
        self.assertIn("build-tool build archive policy", printed)
        # The reason is stated in words the reader can act on, not the verifier's
        # (the cross-reviewer, 2026-09-13, mc-9jd4t).
        self.assertIn("Not ready for a decision yet", printed)
        self.assertNotIn("card contract", printed)
        self.assertNotIn("Decide", printed)
        self.assertNotIn("three builds", printed)

    # The reader 2026-09-13: the reading behind a card is a dated document and is
    # not rewritten, so it opens with one line saying when it was written and
    # which of the two to trust when a number differs.
    def test_appendix_opens_with_the_document_date_and_the_as_of_note(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "t@example.com"],
                cwd=root, check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "t"], cwd=root, check=True
            )
            source = root / "note.md"
            source.write_text("# Note\n\nIt was 1.6.4 then.\n")
            subprocess.run(["git", "add", "note.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "n", "--date", "2026-09-11T09:00:00"],
                cwd=root,
                check=True,
                env={
                    **os.environ,
                    "GIT_COMMITTER_DATE": "2026-09-11T09:00:00",
                },
            )
            original = today_page.REPO_ROOT
            try:
                today_page.REPO_ROOT = root
                records = today_page._appendix_as_of_records(source)
            finally:
                today_page.REPO_ROOT = original
        self.assertEqual("Written September 11. Where a number here differs "
                         "from the card, the card is current as of today.",
                         records[0]["text"])
        self.assertEqual("body", records[0]["style"])
        self.assertEqual("blank", records[1]["style"])

    def test_appendix_as_of_line_falls_back_to_the_file_date(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "note.md"
            source.write_text("# Note\n")
            os.utime(source, (1757548800, 1757548800))
            original = today_page.REPO_ROOT
            try:
                today_page.REPO_ROOT = Path(directory)
                written = today_page._document_written_on(source)
            finally:
                today_page.REPO_ROOT = original
        self.assertEqual(
            dt_module.date.fromtimestamp(1757548800).isoformat(), written
        )

    # The cross-reviewer's review, 2026-09-13: no answer card may make reading the
    # appendix a precondition. A card that needs its reading first is a card
    # that failed to carry its own context.
    def test_appendix_reference_is_optional_background_not_homework(self):
        self.assertEqual(
            "Background, optional: pages 4 to 6.",
            today_page._appendix_reference_note(4, 6),
        )
        self.assertEqual(
            "Background, optional: page 4.",
            today_page._appendix_reference_note(4, 4),
        )
        for note in (
            today_page._appendix_reference_note(4, 6),
            today_page._appendix_reference_note(4, 4),
        ):
            self.assertNotIn("then answer", note.lower())
            self.assertNotIn("read page", note.lower())

    def test_mandatory_read_wording_is_refused_wherever_it_appears(self):
        for text in (
            "Read pages 4 to 6, then answer.",
            "Read page 4, then answer.",
            "You must read the design document.",
            "Read the appendix, then answer.",
            "Read this first.",
            "Read the document before answering.",
        ):
            with self.subTest(text=text), self.assertRaises(
                today_page.TodayPageError
            ):
                today_page._refuse_mandatory_read_wording(text, "the card")

    def test_ordinary_card_prose_about_reading_is_not_refused(self):
        for text in (
            "You are deciding whether to read the logs.",
            "The text already tells the author both are easy to drop.",
            "Background, optional: pages 4 to 6.",
        ):
            with self.subTest(text=text):
                today_page._refuse_mandatory_read_wording(text, "the card")

    def test_a_card_demanding_its_reading_first_does_not_render(self):
        ask = (
            "The design is ready. Read pages 4 to 6, then answer. "
            "Recommendation: approve it. "
            "Decide: (1:q1) [yes/no] Approve it? "
            "Row: row_read Read: tests/fixtures/remarkable/reading-sample.md"
        )
        with self.assertRaises(today_page.TodayPageError) as caught:
            today_page._decision_copy(
                {
                    "item_id": "r",
                    "title": "Approve the design",
                    "project": "test",
                    "age": "2026-09-13",
                    "ask": ask,
                },
                render_date="2026-09-13",
            )
        self.assertIn("Background, optional", str(caught.exception))

    def test_segmented_contract_rejects_free_prose_and_bad_numbering(self):
        base = {
            "item_id": "one",
            "title": "Choose",
            "project": "test",
            "age": "2026-07-25",
        }
        for ask in (
            "Only one sentence.",
            "A complete description. Recommendation: yes.",
            "A complete description. Decide: (1:q1) [yes/no] Choose it? Row: row_7 Read: tests/fixtures/remarkable/reading-sample.md",
            (
                "A complete description. Recommendation: yes. "
                "Decide: (2) Choose it?"
            ),
            (
                "A complete description. Recommendation: yes. "
                "Decide: (1:q1) [yes/no] Choose it.  Row: row_8 Read: tests/fixtures/remarkable/reading-sample.md"
            ),
        ):
            with self.subTest(ask=ask), self.assertRaises(today_page.TodayPageError):
                today_page._decision_copy({**base, "ask": ask})

    def test_multicall_card_has_one_entry_and_box_pair_per_question(self):
        decision = {
            "item_id": "multi",
            "title": "Choose three parts",
            "project": "test",
            "age": "2026-07-24; rev 2026-07-25",
            "ask": (
                "Three parts are ready. Each needs its own answer. "
                "Recommendation: approve all three. "
                "Decide: (1:q1) [yes/no] Approve part one? Recommended: yes because part one is ready. "
                "(2:q2) [yes/no] Approve part two? Recommended: yes because part two is ready. "
                "(3:q3) [yes/no] Approve part three? Recommended: yes because part three is ready. Row: row_9 Read: tests/fixtures/remarkable/reading-sample.md"
            ),
        }
        manifest = today_page.build_manifest("2026-07-25", [], [decision], {})
        self.assertEqual(
            [
                today_page._entry_identity(
                    today_page.row_item_id("row_9"), key
                )
                for key in ("q1", "q2", "q3")
            ],
            [entry["entry_id"] for entry in manifest["entries"]],
        )
        self.assertEqual(3, manifest["entry_counts"]["decisions"])
        self.assertEqual(3, len(manifest["cards"][0]["decision_entry_ids"]))
        self.assertEqual(
            ["q1", "q2", "q3"], manifest["cards"][0]["question_keys"]
        )
        for entry in manifest["entries"]:
            self.assertEqual(
                ["YES", "NO", "DEFER", "DISCARD"],
                [box["choice"] for box in entry["boxes"]],
            )

        first = manifest["entries"][0]
        second = manifest["entries"][1]
        result = today_page.resolve(
            manifest,
            {
                "schema_version": manifest["schema_version"],
                "layout_version": manifest["layout_version"],
                "page_id": manifest["page_id"],
                "strokes": [
                    stroke_for(first["boxes"][0]["bbox"], "first", page=first["page"]),
                    stroke_for(second["boxes"][1]["bbox"], "second", page=second["page"]),
                ],
            },
        )
        self.assertEqual(
            [
                (first["entry_id"], 1, "YES"),
                (second["entry_id"], 2, "NO"),
                (manifest["entries"][2]["entry_id"], 3, None),
            ],
            [
                (item["entry_id"], item["decision_number"], item["choice"])
                for item in result["decisions"]
            ],
        )

    def test_entry_ids_survive_a_reworded_question(self):
        """The reader's answer stays attached to the question when we edit its
        wording: state is keyed on the row's question key, not the text."""
        base = {
            "item_id": "multi",
            "title": "Choose three parts",
            "project": "test",
            "age": "2026-07-24; rev 2026-07-25",
            "ask": (
                "Three parts are ready. Each needs its own answer. "
                "Recommendation: approve all three. "
                "Decide: (1:q1) [yes/no] Approve part one? "
                "Row: row_10 Read: tests/fixtures/remarkable/reading-sample.md"
            ),
        }
        reworded = {
            **base,
            "ask": base["ask"].replace(
                "Approve part one?", "Approve the first part, today?"
            ),
        }
        first = today_page.build_manifest("2026-07-25", [], [base], {})
        second = today_page.build_manifest("2026-07-26", [], [reworded], {})
        self.assertEqual(
            [entry["entry_id"] for entry in first["entries"]],
            [entry["entry_id"] for entry in second["entries"]],
        )
        self.assertEqual(
            first["cards"][0]["question_set_hash"],
            second["cards"][0]["question_set_hash"],
        )

    def test_the_reading_document_follows_its_own_card(self):
        """The reader's interview answer 2: the document a question is about sits
        behind the card, in the same tablet document, in full. The v2
        convention put every appendix after every card and printed an
        "unavailable" page when a file could not be read; v3 holds the row
        instead, and each card's reading follows that card."""
        decisions = [
            {
                "item_id": f"card-{index}",
                "title": f"Approve sample {index}",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "The sample is ready. It needs an answer. "
                    "Recommendation: approve it. "
                    f"Decide: (1:q1) [yes/no] Approve sample {index}? "
                    f"Row: row_11_{index} Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
            for index in range(2)
        ]
        manifest = today_page.build_manifest("2026-07-25", [], decisions, {})
        self.assertEqual(2, len(manifest["appendices"]))
        for card, appendix in zip(manifest["cards"], manifest["appendices"]):
            self.assertEqual(card["card_id"], appendix["card_id"])
            first, last = card["read"]["pages"]
            # the reading starts on the page after its own card, not after
            # every card in the document
            self.assertEqual(card["page_numbers"][-1] + 1, first)
            self.assertEqual([first, last], [
                appendix["page_numbers"][0], appendix["page_numbers"][-1]
            ])
            self.assertEqual(
                "tests/fixtures/remarkable/reading-sample.md",
                card["read"]["path"],
            )
            self.assertEqual(64, len(card["read"]["sha256"]))
        # the second card sits after the first card's reading
        self.assertGreater(
            manifest["cards"][1]["page"], manifest["cards"][0]["read"]["pages"][1]
        )
        lines = [
            line for page in manifest["appendices"][0]["pages"] for line in page
        ]
        headings = [line for line in lines if line["style"] == "h1"]
        self.assertEqual(1, len(headings))
        self.assertNotIn("#", headings[0]["text"])

    def test_a_reading_document_outside_the_repository_holds_the_row(self):
        """The cross-reviewer's finding 6: missing reading material must hold the card
        visibly, not produce an incomplete card."""
        for path in ("../outside.md", "docs/not-there.md", "docs/binary.png"):
            ask = (
                "One file should be read first. It may not be available. "
                "Recommendation: read it before approving. "
                "Decide: (1:q1) [yes/no] Approve the file? "
                f"Row: missing_reading Read: {path}"
            )
            with self.subTest(path=path):
                with self.assertRaises(today_page.TodayPageError):
                    today_page._decision_copy(
                        {
                            "item_id": "missing",
                            "title": "Approve one file",
                            "project": "test",
                            "age": "2026-07-25",
                            "ask": ask,
                        }
                    )

    def test_the_retired_attach_syntax_is_refused(self):
        """Nothing may declare reading material that never gets printed."""
        with self.assertRaisesRegex(today_page.TodayPageError, "Attach"):
            today_page._decision_copy(
                {
                    "item_id": "old",
                    "title": "Approve one file",
                    "project": "test",
                    "age": "2026-07-25",
                    "ask": (
                        "One file is ready. It needs an answer. "
                        "Recommendation: approve it. "
                        "Decide: (1:q1) [yes/no] Approve it? "
                        "Attach: docs/old.md (Old file). "
                        "Row: row_12 Read: tests/fixtures/remarkable/reading-sample.md"
                    ),
                }
            )

    def test_verification_report_is_humanized_and_requires_row_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(
                json.dumps(
                    {
                        "verified_at": "2026-07-28T08:45:44-07:00",
                        "rows": [{"title": "Choose this", "ok": True}],
                    }
                )
            )
            result = today_page.load_verification_report(path)
            self.assertEqual("July 28 at 8:45 AM", result["verified_at_human"])
            path.write_text(
                json.dumps({"verified_at": "2026-07-28T08:45:44-07:00"})
            )
            with self.assertRaisesRegex(today_page.TodayPageError, "row results"):
                today_page.load_verification_report(path)

    def test_older_manifest_layouts_are_refused_cleanly(self):
        # decisions-v2.1 is NOT in this list: it is in
        # ANSWER_COMPATIBLE_LAYOUT_VERSIONS (mc-vjz5) and must validate.
        for old_layout in ("today-v1", "decisions-v2"):
            old = json.loads(json.dumps(self.manifest))
            old["layout_version"] = old_layout
            with self.subTest(layout=old_layout), self.assertRaisesRegex(
                today_page.TodayPageError, "layout version is not supported"
            ):
                today_page.validate_manifest(old)

    def test_answer_compatible_layout_versions_still_validate(self):
        """mc-vjz5: the LAYOUT_VERSION bump for appendix markdown must not
        make validate_manifest refuse manifests from earlier ink-compatible
        layouts -- that broke every archived document's re-resolution.
        """
        for layout in today_page.ANSWER_COMPATIBLE_LAYOUT_VERSIONS:
            old = json.loads(json.dumps(self.manifest))
            old["layout_version"] = layout
            old["manifest_hash"] = today_page.manifest_digest(old)
            with self.subTest(layout=layout):
                today_page.validate_manifest(old)  # must not raise

    def test_manifest_is_stable(self):
        again = today_page.build_manifest(
            "2026-07-25", self.actions, self.decisions, {"review_queue": "abc"}
        )
        self.assertEqual(self.manifest["page_id"], again["page_id"])
        self.assertEqual(today_page.LAYOUT_VERSION, self.manifest["layout_version"])

    def test_manifest_changes_with_content(self):
        changed = today_page.build_manifest(
            "2026-07-25", [], self.decisions, {"review_queue": "abc"}
        )
        self.assertNotEqual(self.manifest["page_id"], changed["page_id"])

    def test_manifest_tampering_stops_resolution(self):
        tampered = json.loads(json.dumps(self.manifest))
        tampered["entries"][0]["label"] = "Changed after generation"
        with self.assertRaises(today_page.TodayPageError):
            today_page.resolve(tampered, self.strokes_doc([]))

    def test_logical_line_origin_maps_to_pdf_top_center(self):
        self.assertEqual(
            [306.0, 792.0],
            today_page.device_to_pdf(0, 0, 1404, 1872),
        )

    def test_logical_line_width_maps_to_pdf_width(self):
        self.assertEqual(
            [0.0, 792.0],
            today_page.device_to_pdf(-960, 0, 1404, 1872),
        )
        self.assertEqual(
            [612.0, 792.0],
            today_page.device_to_pdf(960, 0, 1404, 1872),
        )

    def test_line_points_can_exceed_scene_info_height_and_remain_on_page(self):
        mapped = today_page.device_to_pdf(578.177294, 1901.112353, 1404, 1872)
        self.assertEqual([490.294, 186.02], mapped)

    def test_unknown_coordinate_profile_stops(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError, "unknown coordinate profile"
        ):
            today_page.device_to_pdf(0, 0, 1620, 2160)

    def test_extract_v6_preserves_page_number_for_every_pdf_page(self):
        decisions = [
            {
                "item_id": f"decision-{index}",
                "title": "Choose the archive sample",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "The archive sample is bounded. It is ready. "
                    "Recommendation: approve it. "
                    f"Decide: (1:q1) [yes/no] Approve the archive sample? Row: row_13_{index} Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
            for index in range(8)
        ]
        manifest = today_page.build_manifest("2026-07-25", [], decisions, {})
        self.assertGreater(manifest["page_count"], 1)

        content = {
            "cPages": {
                "pages": [
                    {
                        "id": f"page-{page}",
                        "redir": {"value": page - 1},
                    }
                    for page in range(manifest["page_count"], 0, -1)
                ]
            }
        }
        page_lines = {
            f"page-{page}": [[[float(page), 930], [float(page) + 5, 940]]]
            for page in range(1, manifest["page_count"] + 1)
        }
        result = extract_with_fake_rmscene(manifest, content, page_lines)
        self.assertEqual(
            list(range(1, manifest["page_count"] + 1)),
            [stroke["page"] for stroke in result["strokes"]],
        )
        self.assertEqual(
            [
                f"page-{page}"
                for page in range(1, manifest["page_count"] + 1)
            ],
            [page["rm_page_id"] for page in result["source"]["pages"]],
        )
        self.assertEqual("cPages_redir", result["source"]["page_order_format"])
        self.assertTrue(
            all(
                page["ink_file_status"] == "present"
                and page["line_counts"]
                == {"seen": 1, "kept": 1, "out_of_bounds": 0}
                for page in result["source"]["pages"]
            )
        )

    def test_absent_rm_page_is_recorded_as_unmarked(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        result = extract_with_fake_rmscene(
            manifest,
            {"pages": ["blank-page"]},
            {},
        )
        self.assertEqual([], result["strokes"])
        self.assertEqual(
            {
                "page": 1,
                "rm_page_id": "blank-page",
                "ink_file_status": "absent_assumed_blank",
                "paper_size": None,
                "coordinate_profile": None,
                "line_counts": {"seen": 0, "kept": 0, "out_of_bounds": 0},
            },
            result["source"]["pages"][0],
        )

    def test_both_page_formats_must_agree(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        content = {
            "pages": ["legacy-page"],
            "cPages": {
                "pages": [
                    {
                        "id": "different-page",
                        "redir": {"value": 0},
                    }
                ]
            },
        }
        with self.assertRaisesRegex(today_page.TodayPageError, "formats disagree"):
            extract_with_fake_rmscene(manifest, content, {})

    def test_matching_page_formats_are_verified_together(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        content = {
            "pages": ["same-page"],
            "cPages": {
                "pages": [
                    {
                        "id": "same-page",
                        "redir": {"value": 0},
                    }
                ]
            },
        }
        result = extract_with_fake_rmscene(manifest, content, {})
        self.assertEqual("both_verified", result["source"]["page_order_format"])

    def test_duplicate_legacy_page_ids_stop(self):
        with self.assertRaisesRegex(today_page.TodayPageError, "duplicate page ids"):
            today_page._ordered_archive_page_ids(
                {"pages": ["same-page", "same-page"]}
            )

    def test_unexpected_rm_page_stops(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        with self.assertRaisesRegex(today_page.TodayPageError, "unknown page"):
            extract_with_fake_rmscene(
                manifest,
                {"pages": ["expected-page"]},
                {},
                extra_rm_payloads={
                    "nested/orphan-page.rm": {
                        "paper_size": [1404, 1872],
                        "lines": [],
                    }
                },
            )

    def test_unknown_profile_stops_before_any_strokes_are_returned(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        with self.assertRaisesRegex(today_page.TodayPageError, "unknown coordinate profile"):
            extract_with_fake_rmscene(
                manifest,
                {"pages": ["page-1"]},
                {"page-1": [[[0, 0], [1, 1]]]},
                paper_sizes={"page-1": (1620, 2160)},
            )

    def test_bounds_filter_counts_kept_and_rejected_lines(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        result = extract_with_fake_rmscene(
            manifest,
            {"pages": ["page-1"]},
            {
                "page-1": [
                    [[600, 700], [620, 720]],
                    [[3000, 700], [3020, 720]],
                ]
            },
        )
        self.assertEqual(1, len(result["strokes"]))
        self.assertEqual(
            {"seen": 2, "kept": 1, "out_of_bounds": 1},
            result["source"]["pages"][0]["line_counts"],
        )

    def test_daily_empty_page_manifest_is_valid(self):
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        self.assertEqual(
            {
                "actions": 0,
                "cards": 0,
                "decisions": 0,
                "excluded_already_answered": 0,
                "held": 0,
                "settled": 0,
            },
            manifest["entry_counts"],
        )
        # The cover page still prints: it is the page that says so.
        self.assertEqual(1, manifest["page_count"])
        self.assertEqual([1], manifest["cover_pages"])

    def test_twenty_decisions_render_without_more_than_four_cards_per_page(self):
        decisions = [
            {
                "item_id": f"decision-{index}",
                "title": f"Choose sample {index}",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "The sample is bounded. It is ready. "
                    "Recommendation: approve it. "
                    f"Decide: (1:q1) [yes/no] Approve the sample? Row: row_14_{index} Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
            for index in range(20)
        ]
        manifest = today_page.build_manifest("2026-07-25", [], decisions, {})
        self.assertEqual(20, len(manifest["cards"]))
        # v3: the cover, then each card on its own page, then that card's
        # reading. No card shares a page with another card.
        pages = [card["page"] for card in manifest["cards"]]
        self.assertEqual(len(pages), len(set(pages)))
        self.assertNotIn(1, pages)
        for card in manifest["cards"]:
            first, last = card["read"]["pages"]
            self.assertEqual(card["page_numbers"][-1] + 1, first)
            self.assertLessEqual(first, last)

    def test_every_entry_has_page_local_boxes(self):
        decisions = [
            {
                "item_id": f"decision-{index}",
                "title": f"Choose sample {index}",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "The sample is bounded. It is ready. "
                    "Recommendation: approve it. "
                    f"Decide: (1:q1) [yes/no] Approve the sample? Row: row_15_{index} Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
            for index in range(12)
        ]
        manifest = today_page.build_manifest("2026-07-25", [], decisions, {})
        for entry in manifest["entries"]:
            self.assertIn(entry["page"], range(1, manifest["page_count"] + 1))
            for entry_box in entry["boxes"]:
                x0, y0, x1, y1 = entry_box["bbox"]
                self.assertTrue(0 <= x0 < x1 <= today_page.PAGE_W)
                self.assertTrue(0 <= y0 < y1 <= today_page.PAGE_H)

    def test_same_coordinates_on_different_pages_use_stroke_page(self):
        decisions = [
            {
                "item_id": f"decision-{index}",
                "title": "Choose the repeated sample",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "The repeated sample is bounded. It is ready. "
                    "Recommendation: approve it. "
                    f"Decide: (1:q1) [yes/no] Approve the repeated sample? Row: row_16_{index} Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
            for index in range(14)
        ]
        manifest = today_page.build_manifest("2026-07-25", [], decisions, {})
        by_bbox = {}
        pair = None
        for entry in manifest["entries"]:
            bounds = tuple(entry["boxes"][0]["bbox"])
            if bounds in by_bbox and by_bbox[bounds]["page"] != entry["page"]:
                pair = (by_bbox[bounds], entry)
                break
            by_bbox[bounds] = entry
        self.assertIsNotNone(pair)
        earlier, later = pair
        strokes = {
            "schema_version": manifest["schema_version"],
            "layout_version": manifest["layout_version"],
            "page_id": manifest["page_id"],
            "strokes": [
                stroke_for(
                    later["boxes"][0]["bbox"],
                    page=later["page"],
                )
            ],
        }
        result = today_page.resolve(manifest, strokes)
        answered = [item for item in result["decisions"] if item["choice"] is not None]
        self.assertEqual([later["entry_id"]], [item["entry_id"] for item in answered])
        self.assertNotEqual(earlier["entry_id"], answered[0]["entry_id"])

    def test_missing_stroke_page_is_rejected(self):
        bounds = self.find_decision_box("YES")
        stroke = stroke_for(bounds)
        del stroke["page"]
        strokes = {
            "schema_version": self.manifest["schema_version"],
            "layout_version": self.manifest["layout_version"],
            "page_id": self.manifest["page_id"],
            "strokes": [stroke],
        }
        with self.assertRaisesRegex(today_page.TodayPageError, "1-based page number"):
            today_page.resolve(self.manifest, strokes)

    def test_write_in_ink_is_ignored_without_blocking_tick(self):
        entry = self.decision_entry()
        note = {
            "stroke_id": "long-note",
            "page": entry["page"],
            "points": [
                [300.0, entry["boxes"][0]["bbox"][1]],
                [350.0, entry["boxes"][0]["bbox"][1]],
            ],
        }
        tick = stroke_for(
            entry["boxes"][0]["bbox"], stroke_id="yes", page=entry["page"]
        )
        result = today_page.resolve(self.manifest, self.strokes_doc([note, tick]))
        self.assertEqual(["long-note"], result["ignored_strokes"])
        self.assertEqual("YES", result["decisions"][0]["choice"])

    def test_the_write_in_line_belongs_to_its_own_question(self):
        """The reader's interview answer 1: a lettered choice and an open question
        each get their own line. On the 2026-09-01 sheet three questions
        shared one note line, so an answer could not be told from a comment."""
        decisions = [
            {
                "item_id": "forms",
                "title": "Choose the forms",
                "project": "test",
                "age": "2026-09-01",
                "ask": (
                    "The forms are ready. Each question needs its own. "
                    "Recommendation: use all three. "
                    "Decide: (1:plain) [yes/no] Go ahead? Recommended: yes because it is ready. "
                    "(2:pick) [choice: A = first, B = second] Which one? "
                    "Recommended: A because the first is cheaper. "
                    "(3:open) [write-in] When? Recommended: this week because the work is stopped until then. "
                    "Row: row_17 Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
        ]
        manifest = today_page.build_manifest("2026-09-01", [], decisions, {})
        by_key = {entry["question_key"]: entry for entry in manifest["entries"]}
        self.assertIsNone(by_key["plain"]["write_in"])
        lines = []
        for key in ("pick", "open"):
            write_in = by_key[key]["write_in"]
            self.assertIsNotNone(write_in)
            lines.append(tuple(write_in["bbox"]))
            # the line sits below its own boxes, not below the whole card
            for answer_box in by_key[key]["boxes"]:
                self.assertGreater(answer_box["bbox"][1], write_in["bbox"][1])
        self.assertEqual(len(lines), len(set(lines)))

    def test_write_in_area_is_at_least_3x_the_old_single_line(self):
        """mc-bcsrx: the reader's pen note (review package page 31, 2026-09-20)
        asked for 2x to 3x the pen writing area. The old single ruled line's
        manifest bbox was [x0-4, y0-10, x1+2, y0+14], 24pt tall - the number
        the handwriting extractor (decision_resolver_v3.render_write_in_pngs)
        actually crops. The new region must be at least 3x that, and the
        extractor must be able to read the WHOLE region: entries whose
        answer_type takes a write-in (choice and write_in) get exactly
        WRITE_IN_LINE_COUNT ruled lines, evenly and comfortably spaced, all
        inside the one bbox recorded on the manifest entry."""
        OLD_SINGLE_LINE_BBOX_HEIGHT = 24.0
        decisions = [
            {
                "item_id": "forms",
                "title": "Choose the forms",
                "project": "test",
                "age": "2026-09-20",
                "ask": (
                    "The forms are ready. Each question needs its own. "
                    "Recommendation: use all three. "
                    "Decide: (1:pick) [choice: A = first, B = a different limit I write in] "
                    "Which one? Recommended: A because the first is cheaper. "
                    "(2:open) [write-in] When? Recommended: this week because the work "
                    "is stopped until then. "
                    "Row: row_18 Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
        ]
        manifest = today_page.build_manifest("2026-09-20", [], decisions, {})
        by_key = {entry["question_key"]: entry for entry in manifest["entries"]}
        for key in ("pick", "open"):
            entry = by_key[key]
            write_in = entry["write_in"]
            self.assertIsNotNone(write_in)
            x0, y0, x1, y1 = write_in["bbox"]
            height = y1 - y0
            self.assertGreaterEqual(
                height,
                3 * OLD_SINGLE_LINE_BBOX_HEIGHT,
                f"{key}: write-in bbox is only {height}pt tall, under 3x the "
                f"old {OLD_SINGLE_LINE_BBOX_HEIGHT}pt single line",
            )
            # the enlarged region still sits below this question's own boxes,
            # never spilling into the boxes above it
            for answer_box in entry["boxes"]:
                self.assertGreater(answer_box["bbox"][1], y1)

        # The manifest card carries the actual drawn geometry (fragments ->
        # blocks), which is what proves the region the PDF draws and the
        # region the manifest records are the same rectangle: the extractor
        # reads the whole taller area, not just where the old single line
        # used to be. File: remarkable/today_page.py,
        # _place_question_geometry (block["write_in_lines"], block["write_in"]).
        card = manifest["cards"][0]
        blocks = [
            block
            for fragment in card["fragments"]
            for block in fragment["blocks"]
            if block.get("kind") == "question"
        ]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertTrue(block["writes_in"])
            lines = block["write_in_lines"]
            self.assertEqual(len(lines), today_page.WRITE_IN_LINE_COUNT)
            self.assertEqual(len(lines), 3)
            ys = [line[1] for line in lines]
            # strictly descending (top to bottom of the page) and never
            # closer together than the old label-to-line gap, so the pen has
            # comfortable, even spacing rather than three cramped lines
            # squeezed into the old row.
            gaps = [ys[i] - ys[i + 1] for i in range(len(ys) - 1)]
            for gap in gaps:
                self.assertGreater(gap, 0)
                self.assertGreaterEqual(gap, 20.0)
            entry = by_key[block["question_key"]]
            x0, y0, x1, y1 = entry["write_in"]["bbox"]
            # every ruled line the PDF draws is inside the bbox the manifest
            # records for that same entry
            for line_x0, line_y, line_x1, _ in lines:
                self.assertGreaterEqual(line_x0, x0)
                self.assertLessEqual(line_x1, x1)
                self.assertGreaterEqual(line_y, y0)
                self.assertLessEqual(line_y, y1)

    def test_multi_form_card_overflow_moves_content_not_truncates_it(self):
        """mc-bcsrx: taller write-in rows push a card's later questions past
        one page more often. The existing pagination must still MOVE that
        content onto the next page rather than lose it - a card with five
        write-in questions (five enlarged rows) must still print every
        question's boxes and full write-in area, spread over as many pages
        as it needs."""
        options = ", ".join(
            f"{letter} = option {letter.lower()}" for letter in "AB"
        )
        forms = " ".join(
            f"({index}:q{index}) [choice: {options}] Question number {index}? "
            f"Recommended: A because it is the default."
            for index in range(1, 6)
        )
        decisions = [
            {
                "item_id": "overflow",
                "title": "Five write-in questions",
                "project": "test",
                "age": "2026-09-20",
                "ask": (
                    "Five separate questions, each with its own write-in row. "
                    "Recommendation: work through them in order. "
                    f"Decide: {forms} "
                    "Row: row_19 Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
        ]
        manifest = today_page.build_manifest("2026-09-20", [], decisions, {})
        card = manifest["cards"][0]
        # every question actually printed, none dropped, spread across
        # however many pages the taller rows now need
        self.assertEqual(len(card["decision_entry_ids"]), 5)
        self.assertGreater(len(card["page_numbers"]), 1)
        entries_by_id = {entry["entry_id"]: entry for entry in manifest["entries"]}
        pages_seen = set()
        for entry_id in card["decision_entry_ids"]:
            entry = entries_by_id[entry_id]
            self.assertIn(entry["page"], card["page_numbers"])
            pages_seen.add(entry["page"])
            write_in = entry["write_in"]
            self.assertIsNotNone(write_in)
            self.assertGreaterEqual(write_in["bbox"][3] - write_in["bbox"][1], 72.0)
        # confirms the overflow is real: not every question landed on the
        # same page
        self.assertGreater(len(pages_seen), 1)
        # _assert_v3_bounds (build_manifest -> _layout_v3_document) already
        # raises if any box prints off its own card or off the page; reaching
        # this line is itself proof nothing was clipped.

    def test_generate_uses_decisions_names(self):
        original = today_page.render_pdf
        today_page.render_pdf = lambda path, manifest: path.write_bytes(b"pdf")
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                queue = base / "REVIEW-QUEUE.md"
                report = base / "verify-report.json"
                queue.write_text(
                    "# Queue\n\n## Active - First\n\n"
                    "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                    "| Choose this | A | 2026-07-25 | It is ready. "
                    "It needs an answer. Recommendation: approve it. "
                    "Decide: (1:q1) [yes/no] Approve it? Row: row_18 Read: tests/fixtures/remarkable/reading-sample.md |\n"
                )
                report.write_text(
                    json.dumps(
                        {
                            "verified_at": "2026-07-25T08:30:00-07:00",
                            "rows": [{"title": "Choose this", "ok": True}],
                            "held": [],
                        }
                    )
                )
                pdf, manifest = today_page.generate(
                    queue,
                    None,
                    report,
                    base / "out",
                    "2026-07-25",
                    state_path=base / "question-state.json",
                )
        finally:
            today_page.render_pdf = original
        self.assertEqual("decisions-2026-07-25.pdf", pdf.name)
        self.assertEqual("decisions-2026-07-25.manifest.json", manifest.name)

    def test_the_cover_never_explains_a_held_item_in_internal_terms(self):
        """mc-9jd4t, the cross-reviewer 2026-09-13: the cover told the reader an item was held
        by "card contract: Each Ask needs exactly one Recommendation section
        and one Decide section". Those are words for whoever fixes the row."""
        banned = (
            "card contract",
            "Ask",
            "Recommendation",
            "Decide",
            "row key",
            "manifest",
            "entry_id",
        )
        reasons = [
            "card contract: Each Ask needs exactly one Recommendation section "
            "and one Decide section.",
            "cites mc-abc which is closed - possibly superseded",
            "cites mc-abc which is unknown to the tracker",
            "referenced file missing: docs/designs/nothing.md",
            "something nobody has written yet",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                plain = today_page._plain_held_reason(reason)
                self.assertTrue(plain.startswith("Not ready for a decision yet"))
                for term in banned:
                    self.assertNotIn(term, plain)
        held = [
            {"title": f"Some held item {index}", "reason": reason}
            for index, reason in enumerate(reasons)
        ]
        blocks = today_page._cover_blocks(held, 3, 400.0)
        printed = " ".join(
            line for block in blocks for line in block.get("lines", [])
        )
        self.assertIn("Not ready for a decision yet", printed)
        for term in banned:
            with self.subTest(term=term):
                self.assertNotIn(term, printed)

    def _supplement_fixture(self, base):
        """Two printable rows and a verify report that passes both."""
        queue = base / "REVIEW-QUEUE.md"
        report = base / "verify-report.json"
        queue.write_text(
            "# Queue\n\n## Active - First\n\n"
            "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
            "| Choose this | A | 2026-07-25 | It is ready. "
            "It needs an answer. Recommendation: approve it. "
            "Decide: (1:q1) [yes/no] Approve it? Row: row_18 "
            "Read: tests/fixtures/remarkable/reading-sample.md |\n"
            "| Choose that | A | 2026-07-25 | It is ready too. "
            "It needs an answer too. Recommendation: approve that. "
            "Decide: (1:q2) [yes/no] Approve that? Row: row_19 "
            "Read: tests/fixtures/remarkable/reading-sample.md |\n"
        )
        report.write_text(
            json.dumps(
                {
                    "verified_at": "2026-07-25T08:30:00-07:00",
                    "rows": [
                        {"title": "Choose this", "ok": True},
                        {"title": "Choose that", "ok": True},
                    ],
                    "held": [],
                }
            )
        )
        return queue, report

    def test_a_supplement_prints_only_the_rows_it_names(self):
        """mc-9jd4t: a second document for the same day, three cards, its own
        name, its own page id, and a first sentence saying what it is."""
        original = today_page.render_pdf
        today_page.render_pdf = lambda path, manifest: path.write_bytes(b"pdf")
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                queue, report = self._supplement_fixture(base)
                full_pdf, full_manifest = today_page.generate(
                    queue, None, report, base / "out", "2026-07-25",
                    state_path=base / "question-state.json",
                )
                pdf, manifest = today_page.generate(
                    queue, None, report, base / "out", "2026-07-25",
                    state_path=base / "question-state.json",
                    only_rows=["row_19"],
                    document_name="decisions-2026-07-25-supplement",
                    cover_note="This completes today's document.",
                )
                full = json.loads(full_manifest.read_text())
                part = json.loads(manifest.read_text())
        finally:
            today_page.render_pdf = original
        self.assertEqual("decisions-2026-07-25-supplement.pdf", pdf.name)
        self.assertEqual(
            "decisions-2026-07-25-supplement.manifest.json", manifest.name
        )
        self.assertEqual(2, len(full["cards"]))
        self.assertEqual(["row_19"], [card["row_key"] for card in part["cards"]])
        self.assertEqual("decisions-2026-07-25-supplement", part["document_name"])
        self.assertTrue(
            part["page_id"].startswith("decisions-2026-07-25-supplement-")
        )
        self.assertNotEqual(full["page_id"], part["page_id"])
        # The card itself is the same card, not a re-worded one.
        same = next(
            card for card in full["cards"] if card["row_key"] == "row_19"
        )
        self.assertEqual(same["headline"], part["cards"][0]["headline"])
        self.assertEqual(
            same["question_set_hash"], part["cards"][0]["question_set_hash"]
        )
        # The note is the first thing on the cover, before the heading.
        first = part["cover"][0]["blocks"][0]
        self.assertEqual("cover_body", first["kind"])
        self.assertIn("completes today's document", " ".join(first["lines"]))

    def test_a_supplement_refuses_a_row_it_cannot_print(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue, report = self._supplement_fixture(base)
            with self.assertRaises(today_page.TodayPageError) as caught:
                today_page.generate(
                    queue, None, report, base / "out", "2026-07-25",
                    state_path=base / "question-state.json",
                    only_rows=["row_19", "row_not_here"],
                    document_name="decisions-2026-07-25-supplement",
                )
        self.assertIn("row_not_here", str(caught.exception))

    def test_settled_history_finds_a_supplement_manifest_in_the_same_day(self):
        """An answer penned on a supplement names a page id the day's main
        manifest does not carry; the day's own directory is searched for the
        manifest that does."""
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory)
            (day / "decisions-2026-07-25.manifest.json").write_text(
                json.dumps({"page_id": "decisions-2026-07-25-aaaaaaaaaaaa"})
            )
            (day / "decisions-2026-07-25-supplement.manifest.json").write_text(
                json.dumps({"page_id": "decisions-2026-07-25-supplement-bbbb"})
            )
            fallback = {"page_id": "decisions-2026-07-25-aaaaaaaaaaaa"}
            found = today_page._manifest_for_page_id(
                day, "decisions-2026-07-25-supplement-bbbb", fallback
            )
            self.assertEqual("decisions-2026-07-25-supplement-bbbb", found["page_id"])
            missing = today_page._manifest_for_page_id(
                day, "decisions-2026-07-25-nothing", fallback
            )
            self.assertEqual(fallback, missing)

    def test_generate_rejects_queue_row_not_marked_current(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue = base / "REVIEW-QUEUE.md"
            report = base / "verify-report.json"
            queue.write_text(
                "# Queue\n\n## Active - First\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                "| Choose this | A | 2026-07-25 | It is ready. "
                "It needs an answer. Recommendation: approve it. "
                "Decide: (1:q1) [yes/no] Approve it? Row: row_19 Read: tests/fixtures/remarkable/reading-sample.md |\n"
            )
            report.write_text(
                json.dumps(
                    {
                        "verified_at": "2026-07-25T08:30:00-07:00",
                        "rows": [{"title": "Choose this", "ok": False}],
                        "held": [{"title": "Choose this"}],
                    }
                )
            )
            with self.assertRaisesRegex(
                today_page.TodayPageError,
                "not verified current: Choose this",
            ):
                today_page.generate(
                    queue,
                    None,
                    report,
                    base / "out",
                    "2026-07-25",
                    state_path=base / "question-state.json",
                )

    # --- R3 finding 3: the generator's queue snapshot -----------------------

    @staticmethod
    def _race_fixture(base: Path) -> tuple[Path, Path]:
        """A queue with two answerable Active rows and a usable Closed table."""
        queue = base / "REVIEW-QUEUE.md"
        report = base / "verify-report.json"
        queue.write_text(
            "# Queue\n\n## Active - First\n\n"
            "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
            "| Choose this | A | 2026-07-25 | It is ready. It needs an answer. "
            "Recommendation: approve it. Decide: (1:q1) [yes/no] Approve it? Row: row_20 Read: tests/fixtures/remarkable/reading-sample.md |\n"
            "| And choose this too | B | 2026-07-25 | It is also ready. It "
            "needs an answer. Recommendation: approve it. "
            "Decide: (1:q1) [yes/no] Approve it too? Row: row_21 Read: tests/fixtures/remarkable/reading-sample.md |\n"
            "\n## Closed\n\n"
            "| Item | Closed | Disposition |\n|---|---|---|\n"
            "| Something older | 2026-07-01 | Handled. |\n"
        )
        report.write_text(
            json.dumps(
                {
                    "verified_at": "2026-07-25T08:30:00-07:00",
                    "rows": [
                        {"title": "Choose this", "ok": True},
                        {"title": "And choose this too", "ok": True},
                    ],
                    "held": [],
                }
            )
        )
        return queue, report

    def test_generate_cannot_attest_a_hash_that_disagrees_with_its_rows(self):
        """A row closed between the parse and the hash forged the attestation.

        generate() parsed the queue and hashed the queue as two separate
        reads. A writer landing in between produced a page that printed the
        OLD rows under a hash of the NEW file: a question re-asked while the
        manifest swore its input was current. The rows printed and the hash
        attested must come from one snapshot (mc-63x2).
        """
        original = today_page.render_pdf
        today_page.render_pdf = lambda path, manifest: path.write_bytes(b"pdf")
        real_read_bytes, real_read_text = Path.read_bytes, Path.read_text
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                queue, report = self._race_fixture(base)
                before = real_read_bytes(queue)
                victim = today_page.parse_review_queue(queue)[0]["item_id"]
                state = {"closed": False}

                def close_once():
                    if state["closed"]:
                        return
                    state["closed"] = True
                    today_page.close_review_queue_row(queue, victim)

                def read_bytes(self):
                    data = real_read_bytes(self)
                    if self == queue:
                        close_once()
                    return data

                def read_text(self, *args, **kwargs):
                    data = real_read_text(self, *args, **kwargs)
                    if self == queue:
                        close_once()
                    return data

                Path.read_bytes, Path.read_text = read_bytes, read_text
                try:
                    _, manifest_path = today_page.generate(
                        queue,
                        None,
                        report,
                        base / "out",
                        "2026-07-25",
                        state_path=base / "question-state.json",
                    )
                finally:
                    Path.read_bytes, Path.read_text = real_read_bytes, real_read_text

                self.assertTrue(state["closed"], "the racing writer never ran")
                after = real_read_bytes(queue)
                self.assertNotEqual(before, after, "the row was not really closed")
                manifest = json.loads(real_read_text(manifest_path, encoding="utf-8"))
                printed = [card["item_id"] for card in manifest["cards"]]
                attested = manifest["source_hashes"]["review_queue"]
                # Parsed from copies through the public parser, so this test
                # runs unchanged against the version that had the race.
                candidates = {}
                for label, snapshot in (("before", before), ("after", after)):
                    copy_path = base / f"queue-{label}.md"
                    copy_path.write_bytes(snapshot)
                    candidates[today_page.sha256_bytes(snapshot)] = [
                        row["item_id"]
                        for row in today_page.parse_review_queue(copy_path)
                    ]
                self.assertIn(
                    attested,
                    candidates,
                    "the manifest attests a queue state that never existed",
                )
                self.assertEqual(
                    candidates[attested],
                    printed,
                    "the manifest attests a hash of a queue that does not "
                    "contain the rows the page printed",
                )
        finally:
            today_page.render_pdf = original

    def test_generate_completes_with_the_queue_lock_free(self):
        original = today_page.render_pdf
        today_page.render_pdf = lambda path, manifest: path.write_bytes(b"pdf")
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                queue, report = self._race_fixture(base)
                started = time.monotonic()
                pdf, manifest_path = today_page.generate(
                    queue,
                    None,
                    report,
                    base / "out",
                    "2026-07-25",
                    state_path=base / "question-state.json",
                )
                elapsed = time.monotonic() - started
                self.assertTrue(pdf.is_file() and manifest_path.is_file())
                self.assertLess(elapsed, 10.0, "the 06:00 generator got slow")
        finally:
            today_page.render_pdf = original

    def test_generate_fails_loudly_when_another_process_holds_the_lock(self):
        """Never a hang. The 06:00 job must fail out loud, not wedge."""
        original = today_page.render_pdf
        today_page.render_pdf = lambda path, manifest: path.write_bytes(b"pdf")
        holder = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                queue, report = self._race_fixture(base)
                lock_path = today_page.queue_lock_path(queue)
                code = (
                    "import fcntl, sys, time\n"
                    f"handle = open({str(lock_path)!r}, 'w')\n"
                    "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                    "print('locked', flush=True)\n"
                    "time.sleep(60)\n"
                )
                holder = subprocess.Popen(
                    [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
                )
                self.assertEqual("locked\n", holder.stdout.readline())
                started = time.monotonic()
                with self.assertRaisesRegex(
                    today_page.TodayPageError, "another writer has held"
                ):
                    today_page.generate(
                        queue,
                        None,
                        report,
                        base / "out",
                        "2026-07-25",
                        state_path=base / "question-state.json",
                        lock_timeout=1.0,
                    )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 15.0, "the generator hung on the lock")
                self.assertFalse((base / "out").exists())
        finally:
            today_page.render_pdf = original
            if holder is not None:
                holder.kill()
                holder.wait(timeout=10)


    def test_one_tick_resolves_action(self):
        bounds = self.find_box("action-01", "RUN")
        result = today_page.resolve(self.manifest, self.strokes_doc([stroke_for(bounds)]))
        self.assertEqual("resolved", result["status"])
        self.assertEqual("ack_item", result["actions"][0]["action"]["type"])

    def test_yes_tick_resolves_decision(self):
        bounds = self.find_decision_box("YES")
        result = today_page.resolve(self.manifest, self.strokes_doc([stroke_for(bounds)]))
        self.assertEqual("YES", result["decisions"][0]["choice"])

    def test_no_tick_resolves_decision(self):
        bounds = self.find_decision_box("NO")
        result = today_page.resolve(self.manifest, self.strokes_doc([stroke_for(bounds)]))
        self.assertEqual("NO", result["decisions"][0]["choice"])

    def test_twenty_four_independent_ticks_hit_the_expected_box(self):
        entries = [entry for entry in self.manifest["entries"] for box in entry["boxes"]]
        fixtures = []
        for number in range(24):
            entry = entries[number % len(entries)]
            box = entry["boxes"][number % len(entry["boxes"])]
            fixtures.append((entry, box, number))
        for entry, entry_box, number in fixtures:
            with self.subTest(fixture=number):
                result = today_page.resolve(
                    self.manifest,
                    self.strokes_doc(
                        [
                            stroke_for(
                                entry_box["bbox"],
                                stroke_id=f"fixture-{number}",
                                page=entry["page"],
                            )
                        ]
                    ),
                )
                resolved = result["actions"] + [
                    item
                    for item in result["decisions"]
                    if item["choice"] is not None
                ]
                self.assertEqual(entry["entry_id"], resolved[0]["entry_id"])
                self.assertEqual(entry_box["choice"], resolved[0]["choice"])

    def test_unticked_page_takes_no_action(self):
        result = today_page.resolve(self.manifest, self.strokes_doc([]))
        self.assertEqual([], result["actions"])
        self.assertTrue(
            all(item["status"] == "unanswered" for item in result["decisions"])
        )

    def test_outside_mark_is_ignored(self):
        result = today_page.resolve(
            self.manifest,
            self.strokes_doc([{"stroke_id": "note", "points": [[100, 100], [110, 110]]}]),
        )
        self.assertEqual(["note"], result["ignored_strokes"])

    def test_large_mark_needs_review(self):
        # mc-z5hy3: the mark sits on page 2, an ANSWERABLE page. There a giant
        # mark could be a ruined tick, so it still fails closed. The same mark
        # on a page with no controls (the cover) is handwriting, not an
        # ambiguity - see test_large_mark_on_a_page_without_controls_is_a_note.
        result = today_page.resolve(
            self.manifest,
            self.strokes_doc(
                [
                    {
                        "stroke_id": "large",
                        "page": 2,
                        "points": [[400, 740], [600, 780]],
                    }
                ]
            ),
        )
        self.assertEqual("needs_review", result["status"])
        self.assertEqual("mark_too_large", result["ambiguities"][0]["reason"])

    def test_large_mark_on_a_page_without_controls_is_a_note(self):
        # mc-z5hy3: on 2026-09-03 the underline in the reader's cover note was
        # quarantined as an oversized mark and its 160 strokes reached nobody.
        # A page carrying no answer boxes has nothing to ruin, so the ink is
        # note handwriting and gets a crop.
        result = today_page.resolve(
            self.manifest,
            self.strokes_doc([{"stroke_id": "large", "points": [[400, 740], [600, 780]]}]),
        )
        self.assertEqual("resolved", result["status"])
        self.assertEqual([], result["ambiguities"])
        self.assertEqual(
            [["large"]], [note["stroke_ids"] for note in result["note_regions"]]
        )

    def test_mark_crossing_two_boxes_needs_review(self):
        yes = self.find_decision_box("YES")
        no = self.find_decision_box("NO")
        result = today_page.resolve(
            self.manifest,
            self.strokes_doc(
                [
                    {
                        "stroke_id": "wide",
                        "page": yes.page,
                        "points": [[yes[0], yes[1]], [no[2], no[3]]],
                    }
                ]
            ),
        )
        self.assertEqual("mark_crosses_boxes", result["ambiguities"][0]["reason"])

    def test_real_ticks_do_not_cross_adjacent_subquestion_boxes_through_tolerance(self):
        for fixture in ADJACENT_BOX_TICKS:
            with self.subTest(stroke=fixture["source_stroke_id"]):
                manifest = self.adjacent_box_fixture_manifest(fixture)
                intended, adjacent = manifest["entries"]

                stroke_bbox = fixture["stroke_bbox"]
                intended_yes = fixture["intended_boxes"][0]["bbox"]
                intended_no = fixture["intended_boxes"][1]["bbox"]
                adjacent_yes = fixture["adjacent_boxes"][0]["bbox"]
                gap = stroke_bbox[1] - adjacent_yes[3]
                self.assertGreater(gap, 0.0)
                self.assertLess(gap, today_page.BOX_TOLERANCE)

                def overlap_area(left, right):
                    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
                    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
                    return width * height

                intended_area = (
                    (intended_yes[2] - intended_yes[0])
                    * (intended_yes[3] - intended_yes[1])
                )
                self.assertGreaterEqual(
                    overlap_area(stroke_bbox, intended_yes) / intended_area,
                    0.97,
                )
                self.assertEqual(0.0, overlap_area(stroke_bbox, intended_no))
                self.assertEqual(0.0, overlap_area(stroke_bbox, adjacent_yes))

                strokes = self.fixture_strokes(
                    manifest, fixture["source_stroke_id"], stroke_bbox
                )
                result = today_page.resolve(manifest, strokes)
                self.assertEqual([], result["ambiguities"])
                self.assertEqual(
                    [(intended["entry_id"], "YES")],
                    [
                        (item["entry_id"], item["choice"])
                        for item in result["decisions"]
                        if item["choice"] is not None
                    ],
                )

    def test_centered_mark_touching_adjacent_box_still_needs_review(self):
        fixture = ADJACENT_BOX_TICKS[0]
        manifest = self.adjacent_box_fixture_manifest(fixture)
        adjacent_yes = fixture["adjacent_boxes"][0]["bbox"]
        crossing_bbox = list(fixture["stroke_bbox"])
        crossing_bbox[1] = adjacent_yes[3] - 0.1
        strokes = self.fixture_strokes(manifest, "raw-crossing", crossing_bbox)

        result = today_page.resolve(manifest, strokes)

        self.assertTrue(
            all(item["status"] == "needs_review" for item in result["decisions"])
        )
        self.assertEqual("mark_crosses_boxes", result["ambiguities"][0]["reason"])
        self.assertEqual(
            sorted(entry["entry_id"] for entry in manifest["entries"]),
            result["ambiguities"][0]["candidate_entry_ids"],
        )

    def test_margin_center_disagreeing_with_raw_contact_needs_review(self):
        fixture = ADJACENT_BOX_TICKS[0]
        manifest = self.adjacent_box_fixture_manifest(fixture)
        tolerance = today_page.BOX_TOLERANCE
        intended, adjacent = manifest["entries"]
        intended["boxes"][0]["bbox"] = [100.0, 100.0, 104.0, 104.0]
        adjacent["boxes"][0]["bbox"] = [
            100.0 + tolerance / 2,
            100.0 + tolerance / 2,
            104.0 + tolerance / 2,
            104.0 + tolerance / 2,
        ]
        manifest["manifest_hash"] = today_page.manifest_digest(manifest)
        center_y = 104.0 + tolerance + 0.1
        stroke_bbox = [
            100.0 - tolerance / 2,
            101.0,
            100.0,
            2 * center_y - 101.0,
        ]
        strokes = self.fixture_strokes(manifest, "center-raw-disagree", stroke_bbox)

        result = today_page.resolve(manifest, strokes)

        self.assertTrue(
            all(item["status"] == "needs_review" for item in result["decisions"])
        )
        self.assertEqual("mark_crosses_boxes", result["ambiguities"][0]["reason"])
        self.assertEqual(
            sorted(entry["entry_id"] for entry in manifest["entries"]),
            result["ambiguities"][0]["candidate_entry_ids"],
        )

    def test_yes_and_no_ticks_need_review(self):
        yes = self.find_decision_box("YES")
        no = self.find_decision_box("NO")
        result = today_page.resolve(
            self.manifest,
            self.strokes_doc([stroke_for(yes, "yes"), stroke_for(no, "no")]),
        )
        self.assertEqual("multiple_choices", result["ambiguities"][0]["reason"])
        self.assertEqual("needs_review", result["decisions"][0]["status"])

    def test_page_id_mismatch_stops(self):
        strokes = self.strokes_doc([])
        strokes["page_id"] = "wrong"
        with self.assertRaises(today_page.TodayPageError):
            today_page.resolve(self.manifest, strokes)

    def test_layout_mismatch_stops(self):
        strokes = self.strokes_doc([])
        strokes["layout_version"] = "wrong"
        with self.assertRaises(today_page.TodayPageError):
            today_page.resolve(self.manifest, strokes)

    def test_schema_mismatch_stops(self):
        strokes = self.strokes_doc([])
        strokes["schema_version"] = 99
        with self.assertRaises(today_page.TodayPageError):
            today_page.resolve(self.manifest, strokes)

    def test_bad_stroke_points_stop(self):
        with self.assertRaises(today_page.TodayPageError):
            today_page.resolve(
                self.manifest,
                self.strokes_doc([{"stroke_id": "bad", "points": [["x", None]]}]),
            )

    def test_execute_defaults_to_dry_run(self):
        bounds = self.find_box("action-01", "RUN")
        resolution = today_page.resolve(
            self.manifest, self.strokes_doc([stroke_for(bounds)])
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            result = today_page.execute(
                resolution, base / "receipts", base / "state.json", None, apply=False
            )
            self.assertEqual("dry_run", result["results"][0]["status"])
            self.assertFalse((base / "receipts").exists())

    def test_apply_writes_state_and_idempotent_receipt(self):
        bounds = self.find_box("action-01", "RUN")
        resolution = today_page.resolve(
            self.manifest, self.strokes_doc([stroke_for(bounds)])
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = today_page.execute(
                resolution, base / "receipts", base / "state.json", None, apply=True
            )
            second = today_page.execute(
                resolution, base / "receipts", base / "state.json", None, apply=True
            )
            state = json.loads((base / "state.json").read_text())
            receipts = list((base / "receipts").glob("*.json"))
        self.assertEqual("done", first["results"][0]["status"])
        self.assertEqual("already_done", second["results"][0]["status"])
        self.assertIn("acknowledged_at", state["items"]["summary-1"])
        self.assertEqual(1, len(receipts))

    def test_later_unrelated_ink_cannot_repeat_an_action(self):
        bounds = self.find_box("action-01", "RUN")
        first_resolution = today_page.resolve(
            self.manifest, self.strokes_doc([stroke_for(bounds)])
        )
        later_resolution = today_page.resolve(
            self.manifest,
            self.strokes_doc(
                [
                    stroke_for(bounds),
                    {"stroke_id": "later-note", "points": [[100, 100], [110, 110]]},
                ]
            ),
        )
        self.assertNotEqual(first_resolution["ink_hash"], later_resolution["ink_hash"])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = today_page.execute(
                first_resolution,
                base / "receipts",
                base / "state.json",
                None,
                apply=True,
            )
            second = today_page.execute(
                later_resolution,
                base / "receipts",
                base / "state.json",
                None,
                apply=True,
            )
            receipts = list((base / "receipts").glob("*.json"))
        self.assertEqual("done", first["results"][0]["status"])
        self.assertEqual("already_done", second["results"][0]["status"])
        self.assertEqual(1, len(receipts))

    def test_approve_queue_row_moves_exact_active_row_to_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue = base / "REVIEW-QUEUE.md"
            queue.write_text(
                "# Queue\n\n"
                "## Active - First\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                "| [One](one.md) | A | 2026-07-25 | One item is ready. "
                "It needs a choice. Recommendation: approve one. "
                "Decide: (1:q1) [yes/no] Approve one? Row: row_22 Read: tests/fixtures/remarkable/reading-sample.md |\n"
                "| Two | B | 2026-07-25 | Another item remains. "
                "It needs a choice. Recommendation: leave it. "
                "Decide: (1:q1) [yes/no] Leave this row? Row: row_23 Read: tests/fixtures/remarkable/reading-sample.md |\n\n"
                "## Closed\n\n"
                "| Item | Closed | Disposition |\n|---|---|---|\n"
                "| Old | yesterday | Already done. |\n"
            )
            target = today_page.parse_review_queue(queue)[0]
            actions = [
                {
                    "type": "approve_queue_row",
                    "label": "Approve one",
                    "payload": {"item_id": target["item_id"]},
                }
            ]
            manifest = today_page.build_manifest(
                "2026-07-25", actions, [], {"review_queue": "fixture"}
            )
            bounds = manifest["entries"][0]["boxes"][0]["bbox"]
            strokes = {
                "schema_version": manifest["schema_version"],
                "layout_version": manifest["layout_version"],
                "page_id": manifest["page_id"],
                "strokes": [
                    stroke_for(bounds, page=manifest["entries"][0]["page"])
                ],
            }
            resolution = today_page.resolve(manifest, strokes)
            result = today_page.execute(
                resolution,
                base / "receipts",
                base / "state.json",
                None,
                queue,
                apply=True,
            )
            updated = queue.read_text()
        self.assertEqual("done", result["results"][0]["status"])
        active_text, closed_text = updated.split("## Closed", 1)
        self.assertNotIn("[One](one.md)", active_text)
        self.assertIn("| Two | B |", active_text)
        self.assertIn("[One](one.md)", closed_text)
        self.assertIn("DECIDED by Decisions document", closed_text)
        closed_rows = [line for line in closed_text.splitlines() if line.startswith("|")]
        header_cells = [cell.strip() for cell in closed_rows[0].strip("|").split("|")]
        inserted = next(line for line in closed_rows if "[One](one.md)" in line)
        inserted_cells = [cell.strip() for cell in inserted.strip("|").split("|")]
        self.assertEqual(len(header_cells), len(inserted_cells))
        self.assertEqual(3, len(inserted_cells))
        self.assertEqual(today_page.dt.date.today().isoformat(), inserted_cells[1])

    def test_concurrent_execution_claim_allows_one_winner(self):
        bounds = self.find_box("action-01", "RUN")
        resolution = today_page.resolve(
            self.manifest, self.strokes_doc([stroke_for(bounds)])
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            barrier = threading.Barrier(2)
            results = []

            def worker():
                barrier.wait()
                results.append(
                    today_page.execute(
                        resolution,
                        base / "receipts",
                        base / "state.json",
                        None,
                        apply=True,
                    )["results"][0]["status"]
                )

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            receipts = list((base / "receipts").glob("*.json"))
        self.assertEqual(1, results.count("done"))
        self.assertEqual(1, len(receipts))
        self.assertEqual({"done", "needs_review"}, set(results))

    def test_ambiguity_no_longer_blocks_execution(self):
        # mc-1jw9 defect 1: fail-closed is per mark, not per document. resolve()
        # already excludes ambiguous entries from "actions"/"decisions", so
        # execute() must not treat a doc-wide ambiguities list as fatal.
        resolution = {"ambiguities": [{"reason": "test"}], "actions": []}
        with tempfile.TemporaryDirectory() as directory:
            result = today_page.execute(
                resolution,
                Path(directory) / "receipts",
                Path(directory) / "state.json",
                None,
                apply=False,
            )
        self.assertEqual({"apply": False, "results": []}, result)

    def test_process_archive_connects_extract_resolve_and_dry_run(self):
        bounds = self.find_box("action-01", "RUN")
        original = today_page.extract_v6
        today_page.extract_v6 = lambda manifest, archive: self.strokes_doc(
            [stroke_for(bounds)]
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                result = today_page.process_archive(
                    self.manifest,
                    base / "fixture.rmdoc",
                    base / "out",
                    base / "receipts",
                    base / "state.json",
                    None,
                    apply=False,
                )
                resolution = json.loads((base / "out" / "resolution.json").read_text())
                execution = json.loads((base / "out" / "execution.json").read_text())
            self.assertEqual("resolved", result["status"])
            self.assertEqual("ack_item", resolution["actions"][0]["action"]["type"])
            self.assertEqual("dry_run", execution["results"][0]["status"])
        finally:
            today_page.extract_v6 = original

    def test_process_archive_quarantines_ambiguity_without_blocking_clean_marks(self):
        # mc-1jw9 defect 1: an ambiguous mark elsewhere on the page must not
        # block a cleanly-ticked, unrelated action from applying.
        bounds = self.find_box("action-01", "RUN")
        original = today_page.extract_v6
        today_page.extract_v6 = lambda manifest, archive: self.strokes_doc(
            [
                stroke_for(bounds, stroke_id="clean-run"),
                # mc-z5hy3: on an answerable page, where a giant mark could be
                # a ruined tick. On the cover it would now be a note region.
                {"stroke_id": "large", "page": 2, "points": [[400, 740], [600, 780]]},
            ]
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                result = today_page.process_archive(
                    self.manifest,
                    base / "fixture.rmdoc",
                    base / "out",
                    base / "receipts",
                    base / "state.json",
                    None,
                    apply=True,
                )
                resolution = json.loads((base / "out" / "resolution.json").read_text())
                execution = json.loads((base / "out" / "execution.json").read_text())
                self.assertEqual("needs_review", result["status"])
                self.assertEqual(
                    "mark_too_large", resolution["ambiguities"][0]["reason"]
                )
                self.assertTrue(
                    all(
                        item["status"] == "unanswered"
                        for item in resolution.get("decisions", [])
                    )
                )
                self.assertEqual("done", execution["results"][0]["status"])
                self.assertEqual(1, len(result["ambiguities"]))
                self.assertTrue((base / "receipts").exists())
        finally:
            today_page.extract_v6 = original

    def test_process_archive_creates_receipts_dir_up_front(self):
        # mc-1jw9 defect 5: never fail on a missing receipts tree.
        original = today_page.extract_v6
        today_page.extract_v6 = lambda manifest, archive: self.strokes_doc([])
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                today_page.process_archive(
                    self.manifest,
                    base / "fixture.rmdoc",
                    base / "out",
                    base / "deeply" / "nested" / "receipts",
                    base / "state.json",
                    None,
                    apply=False,
                )
                self.assertTrue(
                    (base / "deeply" / "nested" / "receipts").is_dir()
                )
        finally:
            today_page.extract_v6 = original

    def test_watcher_is_noop_without_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            result = today_watch.handle_changed(
                ["/Briefs/Today"], Path(directory) / "missing.json"
            )
        self.assertEqual([], result)

    def test_annotation_alert_forbids_duplicate_today_execution(self):
        watcher = (ROOT / "remarkable" / "annotation-watch.sh").read_text()
        self.assertIn(
            "Do not vision-read or execute the same Today-page boxes again.",
            watcher,
        )

    def test_watcher_routes_only_registered_path_and_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = base / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "python": "/test/python",
                        "pages": {
                            "/Briefs/Today": {
                                "manifest": str(base / "manifest.json"),
                                "out_dir": str(base / "out"),
                                "receipts_dir": str(base / "receipts"),
                                "state": str(base / "state.json"),
                            }
                        },
                    }
                )
            )
            calls = []
            original = today_watch.subprocess.run

            def fake_run(argv, **kwargs):
                calls.append(argv)
                if argv[1] == "get":
                    Path(kwargs["cwd"], "today.rmdoc").write_bytes(b"fixture")
                    return subprocess.CompletedProcess(argv, 0)
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout='{"status":"resolved","apply":false}',
                    stderr="",
                )

            today_watch.subprocess.run = fake_run
            try:
                result = today_watch.handle_changed(
                    ["/Briefs/Other", "/Briefs/Today"],
                    registry,
                    rmapi=Path("/test/rmapi"),
                )
            finally:
                today_watch.subprocess.run = original
        self.assertEqual(2, len(calls))
        self.assertNotIn("--apply", calls[1])
        self.assertEqual("/Briefs/Today", result[0]["device_path"])

    def test_registration_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = base / "manifest.json"
            today_page.atomic_json(manifest, self.manifest)
            result = today_page.register_page(
                base / "registry.json",
                "/Briefs/decisions-2026-07-25",
                manifest,
                base / "out",
                base / "receipts",
                base / "state.json",
                "/venv/bin/python",
                None,
                apply=False,
            )
            registry = json.loads((base / "registry.json").read_text())
        self.assertFalse(result["apply"])
        self.assertFalse(registry["pages"]["/Briefs/decisions-2026-07-25"]["apply"])

    # -- mc-1jw9 regression coverage -------------------------------------

    def test_cpages_idx_orders_pages_when_redir_is_partially_missing(self):
        # Root cause: a page appended after the doc's original creation
        # (live 2026-08-02, a fresh re-push) carries no "redir" at all - only
        # "idx". idx alone must resolve order rather than failing the whole
        # document the way the old redir-only parser did.
        content = {
            "cPages": {
                "pages": [
                    {"id": "p1", "idx": {"value": "ba"}, "redir": {"value": 0}},
                    {"id": "p2", "idx": {"value": "bb"}, "redir": {"value": 1}},
                    {"id": "p3", "idx": {"value": "bc"}},
                ]
            }
        }
        ids, fmt = today_page._ordered_archive_page_ids(content)
        self.assertEqual(["p1", "p2", "p3"], ids)
        self.assertEqual("cPages_idx", fmt)

    def test_cpages_idx_and_redir_disagreement_stops(self):
        content = {
            "cPages": {
                "pages": [
                    {"id": "p1", "idx": {"value": "bb"}, "redir": {"value": 0}},
                    {"id": "p2", "idx": {"value": "ba"}, "redir": {"value": 1}},
                ]
            }
        }
        with self.assertRaisesRegex(today_page.TodayPageError, "disagree"):
            today_page._ordered_archive_page_ids(content)

    def test_extra_trailing_page_is_reported_not_fatal(self):
        # mc-1jw9 defect 3: writing past the last manifest page makes the
        # tablet append a new page. That page must be reported, not treated
        # as a whole-document failure.
        manifest = today_page.build_manifest("2026-07-25", [], [], {})
        content = {
            "cPages": {
                "pages": [
                    {"id": "p1", "idx": {"value": "ba"}},
                    {"id": "extra", "idx": {"value": "bb"}},
                ]
            }
        }
        result = extract_with_fake_rmscene(
            manifest, content, {"p1": [], "extra": [[[0, 0], [1, 1]]]}
        )
        self.assertEqual(1, len(result["source"]["pages"]))
        self.assertEqual(
            [{"rm_page_id": "extra", "ink_file_status": "present", "line_count": 1}],
            result["source"]["unplaced_pages"],
        )

    def test_fewer_pages_than_manifest_stops(self):
        decisions = [
            {
                "item_id": f"decision-{index}",
                "title": "Choose the archive sample",
                "project": "test",
                "age": "2026-07-25",
                "ask": (
                    "The archive sample is bounded. It is ready. "
                    "Recommendation: approve it. "
                    f"Decide: (1:q1) [yes/no] Approve the archive sample? Row: row_24_{index} Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
            for index in range(8)
        ]
        manifest = today_page.build_manifest("2026-07-25", [], decisions, {})
        self.assertGreater(manifest["page_count"], 1)
        content = {"cPages": {"pages": [{"id": "only-page", "idx": {"value": "ba"}}]}}
        with self.assertRaisesRegex(today_page.TodayPageError, "expects"):
            extract_with_fake_rmscene(manifest, content, {})

    def test_send_bus_alert_dry_run_never_starts_a_process(self):
        original = today_page.subprocess.run
        today_page.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("dry_run must not invoke a subprocess")
        )
        try:
            result = today_page.send_bus_alert("subj", "body", "slug", dry_run=True)
        finally:
            today_page.subprocess.run = original
        self.assertEqual(
            {"sent": False, "dry_run": True, "subject": "subj", "slug": "slug"}, result
        )

    def test_main_process_archive_reports_input_error_with_distinct_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            alerts = []
            original = today_page.send_bus_alert
            today_page.send_bus_alert = lambda *a, **k: alerts.append((a, k)) or {
                "sent": False,
                "dry_run": True,
            }
            try:
                code = today_page.main(
                    [
                        "process-archive",
                        "--manifest",
                        str(base / "missing-manifest.json"),
                        "--archive",
                        str(base / "missing.rmdoc"),
                        "--out-dir",
                        str(base / "out"),
                        "--receipts-dir",
                        str(base / "receipts"),
                        "--state",
                        str(base / "state.json"),
                        "--alert-dry-run",
                    ]
                )
            finally:
                today_page.send_bus_alert = original
        self.assertEqual(today_page.EXIT_INPUT_ERROR, code)
        self.assertEqual(1, len(alerts))
        self.assertIn("input error", alerts[0][0][0])

    def test_main_process_archive_reports_crash_with_distinct_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest_path = base / "manifest.json"
            today_page.atomic_json(manifest_path, self.manifest)
            original_process = today_page.process_archive
            original_alert = today_page.send_bus_alert
            alerts = []
            today_page.process_archive = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("boom")
            )
            today_page.send_bus_alert = lambda *a, **k: alerts.append((a, k)) or {
                "sent": False,
                "dry_run": True,
            }
            try:
                code = today_page.main(
                    [
                        "process-archive",
                        "--manifest",
                        str(manifest_path),
                        "--archive",
                        str(base / "fixture.rmdoc"),
                        "--out-dir",
                        str(base / "out"),
                        "--receipts-dir",
                        str(base / "receipts"),
                        "--state",
                        str(base / "state.json"),
                        "--alert-dry-run",
                    ]
                )
            finally:
                today_page.process_archive = original_process
                today_page.send_bus_alert = original_alert
        self.assertEqual(today_page.EXIT_CRASH, code)
        self.assertEqual(1, len(alerts))
        self.assertIn("crashed", alerts[0][0][0])

    def test_main_process_archive_alerts_on_handwriting_without_failing_the_run(self):
        # mc-z5hy3: on 2026-09-03 four handwritten notes reached nobody
        # because the v3 path had no note surface. The alert now names them.
        # It stays exit 0: the answers resolved cleanly, and today_watch only
        # runs the consumer that records them when this exits 0.
        bounds = self.find_box("action-01", "RUN")
        original = today_page.extract_v6
        today_page.extract_v6 = lambda manifest, archive: self.strokes_doc(
            [
                stroke_for(bounds, stroke_id="clean-run"),
                {"stroke_id": "note-a", "points": [[80, 300], [140, 306]]},
                {"stroke_id": "note-b", "points": [[80, 275], [150, 281]]},
            ]
        )
        alerts = []
        original_alert = today_page.send_bus_alert
        today_page.send_bus_alert = lambda *a, **k: alerts.append((a, k)) or {
            "sent": False,
            "dry_run": True,
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                manifest_path = base / "manifest.json"
                today_page.atomic_json(manifest_path, self.manifest)
                code = today_page.main(
                    [
                        "process-archive",
                        "--manifest",
                        str(manifest_path),
                        "--archive",
                        str(base / "fixture.rmdoc"),
                        "--out-dir",
                        str(base / "out"),
                        "--receipts-dir",
                        str(base / "receipts"),
                        "--state",
                        str(base / "state.json"),
                        "--alert-dry-run",
                    ]
                )
                resolution = json.loads((base / "out" / "resolution.json").read_text())
                notes = json.loads((base / "out" / "notes.json").read_text())
                crops_written = [
                    Path(note["path"]).is_file() for note in notes
                ]
        finally:
            today_page.extract_v6 = original
            today_page.send_bus_alert = original_alert
        self.assertEqual(today_page.EXIT_OK, code)
        self.assertEqual(1, len(alerts))
        subject, body = alerts[0][0][0], alerts[0][0][1]
        self.assertIn("1 handwritten note(s) need your read", subject)
        self.assertNotIn("mark(s) need your read", subject)
        self.assertIn("Handwritten note lines needing a vision-read", body)
        self.assertEqual(1, len(notes))
        self.assertEqual([True], crops_written)
        # The resolution names the crop, so a reader of it alone can open it.
        self.assertEqual(
            [note["path"] for note in notes],
            [region["path"] for region in resolution["note_regions"]],
        )
        self.assertEqual(
            ["note-a", "note-b"], resolution["note_regions"][0]["stroke_ids"]
        )

    def test_main_process_archive_alerts_and_exits_distinctly_on_ambiguity(self):
        bounds = self.find_box("action-01", "RUN")
        original = today_page.extract_v6
        today_page.extract_v6 = lambda manifest, archive: self.strokes_doc(
            # mc-z5hy3: page 2 is answerable, so this stays an ambiguity.
            [{"stroke_id": "large", "page": 2, "points": [[400, 740], [600, 780]]}]
        )
        alerts = []
        original_alert = today_page.send_bus_alert
        today_page.send_bus_alert = lambda *a, **k: alerts.append((a, k)) or {
            "sent": False,
            "dry_run": True,
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                manifest_path = base / "manifest.json"
                today_page.atomic_json(manifest_path, self.manifest)
                code = today_page.main(
                    [
                        "process-archive",
                        "--manifest",
                        str(manifest_path),
                        "--archive",
                        str(base / "fixture.rmdoc"),
                        "--out-dir",
                        str(base / "out"),
                        "--receipts-dir",
                        str(base / "receipts"),
                        "--state",
                        str(base / "state.json"),
                        "--alert-dry-run",
                    ]
                )
        finally:
            today_page.extract_v6 = original
            today_page.send_bus_alert = original_alert
        self.assertEqual(today_page.EXIT_NEEDS_REVIEW, code)
        self.assertEqual(1, len(alerts))
        subject, body = alerts[0][0][0], alerts[0][0][1]
        self.assertIn("mark(s) need your read", subject)
        self.assertIn("oversized mark", body)

# ---------------------------------------------------------------------------
# mc-rrjv work item 1: the answer geometry the reader's pen actually needs
# ---------------------------------------------------------------------------

# Measured off every archived document, not chosen: the reader's tick marks run
# 15.5-21.7 points tall (median across archives), and across the 237
# already-resolved decision strokes the mark's centre sits up to 10.23 points
# horizontally and 9.24 points vertically away from the centre of the box he
# meant (p95 5.34 and 6.06).
MEASURED_TICK_HEIGHT = 21.7
MEASURED_TICK_WIDTH = 21.7
MEASURED_MAX_CENTRE_OFFSET_X = 10.23
MEASURED_MAX_CENTRE_OFFSET_Y = 9.24


def _sample_decisions(count, questions_per_card=2, project="example-project"):
    decisions = []
    for index in range(count):
        asks = " ".join(
            f"({number}:q{number}) [yes/no] Approve part {number} of sample "
            f"{index}? Recommended: yes because part {number} is ready."
            for number in range(1, questions_per_card + 1)
        )
        decisions.append(
            {
                "item_id": f"sample-{index}",
                "title": f"Approve the bounded sample number {index}",
                "project": project,
                "age": "2026-08-20",
                "ask": (
                    f"Sample {index} is bounded and ready for a choice. "
                    "It has been verified current. "
                    "Recommendation: approve it. "
                    f"Decide: {asks} "
                    f"Row: sample_{index} "
                    "Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            }
        )
    return decisions


class AppendixMarkdownTests(unittest.TestCase):
    """mc-vjz5: appendix attachments render as styled markdown, not raw text."""

    def test_card_specimen_parses_and_renders_before_recommendation(self):
        font_path = "tests/fixtures/remarkable/specimen-font/Vera.ttf"
        decision = today_page._decision_copy({
            "title": "Install the label font?", "project": "test", "age": "2026-10-08",
            "ask": (
                "The font is ready to inspect. Recommendation: install it. "
                "Decide: (1:font) [yes/no] Install it? "
                f"Specimen: {font_path} | PRESS PLAY ON TAPE "
                "Row: c64_font_install Read: tests/fixtures/remarkable/reading-sample.md"
            ),
        })
        self.assertEqual("PRESS PLAY ON TAPE", decision["specimen"]["text"])
        blocks = today_page._card_blocks(
            decision, "",
            today_page.PAGE_W - 2 * today_page.PAGE_MARGIN_X - 2 * today_page.CARD_PADDING,
        )
        kinds = [block["kind"] for block in blocks]
        self.assertLess(kinds.index("leadin"), kinds.index("specimen"))
        self.assertLess(kinds.index("specimen"), kinds.index("recommendation"))
        specimen = next(block for block in blocks if block["kind"] == "specimen")
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "card-specimen.pdf"
            from reportlab.pdfgen import canvas

            pdf = canvas.Canvas(
                str(pdf_path), pagesize=(today_page.PAGE_W, today_page.PAGE_H)
            )
            from reportlab.pdfbase import pdfmetrics

            drawn = []
            draw_string = pdf.drawString

            def record_string(x, y, text):
                drawn.append((text, y, pdf._fontname, pdf._fontsize))
                return draw_string(x, y, text)

            pdf.drawString = record_string
            y_top = 680.0
            today_page._draw_fragment(pdf, {
                "x": today_page.PAGE_MARGIN_X,
                "width": today_page.PAGE_W - 2 * today_page.PAGE_MARGIN_X,
                "top": 700.0, "bottom": 630.0,
                "blocks": [{**specimen, "y_top": y_top}],
            }, outline=False)
            sample = next(item for item in drawn if item[0] == "PRESS PLAY ON TAPE")
            caption = next(item for item in drawn if item[0].startswith("Font:"))
            self.assertLessEqual(
                sample[1] + pdfmetrics.getAscent(sample[2], sample[3]), y_top
            )
            self.assertGreaterEqual(
                caption[1] + pdfmetrics.getDescent(caption[2], caption[3]),
                y_top - specimen["height"],
            )
            pdf.save()
            self.assertIn(b"/FontFile2", pdf_path.read_bytes())
            if shutil.which("pdftotext"):
                result = subprocess.run(
                    ["pdftotext", str(pdf_path), "-"],
                    capture_output=True, text=True, check=True,
                )
                self.assertIn("PRESS PLAY ON TAPE", result.stdout)

    def test_card_without_specimen_keeps_existing_blocks(self):
        decision = today_page._decision_copy({
            "title": "Install the label font?", "project": "test", "age": "2026-10-08",
            "ask": (
                "The font is ready to inspect. Recommendation: install it. "
                "Decide: (1:font) [yes/no] Install it? "
                "Row: c64_font_install Read: tests/fixtures/remarkable/reading-sample.md"
            ),
        })
        self.assertIsNone(decision["specimen"])
        blocks = today_page._card_blocks(
            decision, "",
            today_page.PAGE_W - 2 * today_page.PAGE_MARGIN_X - 2 * today_page.CARD_PADDING,
        )
        self.assertEqual(
            ["headline", "dates", "leadin", "recommendation", "heading", "question"],
            [block["kind"] for block in blocks],
        )

    def test_card_specimen_rejects_font_outside_repository(self):
        with self.assertRaises(today_page.TodayPageError):
            today_page._decision_copy({
                "title": "Install the label font?", "project": "test", "age": "2026-10-08",
                "ask": (
                    "The font is ready. Recommendation: install it. "
                    "Decide: (1:font) [yes/no] Install it? "
                    "Specimen: ../outside.ttf | SAMPLE "
                    "Row: c64_font_install Read: tests/fixtures/remarkable/reading-sample.md"
                ),
            })

    def test_specimen_prints_in_embedded_font_with_caption(self):
        path = "tests/fixtures/remarkable/specimen-font/Vera.ttf"
        records = today_page._markdown_to_line_records(
            f"Specimen: {path} | PRESS PLAY ON TAPE"
        )
        specimen = next(record for record in records if record["style"] == "specimen")
        self.assertEqual("PRESS PLAY ON TAPE", specimen["text"])
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "specimen.pdf"
            from reportlab.pdfgen import canvas

            pdf = canvas.Canvas(str(pdf_path), pagesize=(today_page.PAGE_W, today_page.PAGE_H))
            today_page._draw_appendix_line(pdf, specimen, 600.0)
            pdf.save()
            self.assertIn(b"/FontFile2", pdf_path.read_bytes())
            if shutil.which("pdftotext"):
                result = subprocess.run(
                    ["pdftotext", str(pdf_path), "-"],
                    capture_output=True, text=True, check=True,
                )
                self.assertIn("PRESS PLAY ON TAPE", result.stdout)
                self.assertIn("Font: Vera", result.stdout)

    def test_specimen_rejects_bad_or_missing_font_path(self):
        invalid = [
            "/tmp/font.ttf",
            "../outside.ttf",
            "tests/fixtures/remarkable/specimen-font/bitstream-vera-license.txt",
            "tests/fixtures/remarkable/specimen-font/not-here.ttf",
        ]
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(today_page.TodayPageError):
                today_page._markdown_to_line_records(f"Specimen: {path} | SAMPLE")

    def test_word_boundary_wrapping_does_not_break_mid_word(self):
        text = "word " * 40
        records = today_page._wrap_text_block(text.strip(), "body", indent=0)
        for record in records:
            for token in record["text"].split():
                self.assertEqual("word", token)

    def test_a_single_overlong_word_is_the_only_mid_word_split(self):
        long_word = "x" * 400
        records = today_page._wrap_text_block(long_word, "body", indent=0)
        self.assertGreater(len(records), 1)
        rejoined = "".join(record["text"] for record in records)
        self.assertEqual(long_word, rejoined)

    def test_each_block_type_survives_with_the_right_style(self):
        source = "\n".join(
            [
                "# Heading One",
                "",
                "A body paragraph with **bold** and *italic* and `code` and a "
                "[link](http://example.com).",
                "",
                "- top bullet",
                "  - nested bullet",
                "",
                "1. first ordered",
                "2. second ordered",
                "",
                "> a quoted line",
                "",
                "```",
                "code fence line",
                "```",
                "",
                "| A | B |",
                "|---|---|",
                "| 1 | 2 |",
                "",
                "---",
            ]
        )
        records = today_page._markdown_to_line_records(source)
        styles = {record["style"] for record in records}
        self.assertIn("h1", styles)
        self.assertIn("body", styles)
        self.assertIn("bullet", styles)
        self.assertIn("quote", styles)
        self.assertIn("code", styles)
        self.assertIn("table", styles)
        self.assertIn("rule", styles)

        heading = next(r for r in records if r["style"] == "h1")
        self.assertEqual("Heading One", heading["text"])

        body = next(r for r in records if r["style"] == "body" and "bold" in r["text"])
        self.assertNotIn("**", body["text"])
        self.assertNotIn("*italic*", body["text"])
        self.assertNotIn("`code`", body["text"])
        self.assertNotIn("(http://example.com)", body["text"])
        self.assertIn("bold", body["text"])
        self.assertIn("link", body["text"])

        bullets = [r for r in records if r["style"] == "bullet"]
        self.assertTrue(any(r["indent"] == 0 for r in bullets))
        self.assertTrue(any(r["indent"] == 1 for r in bullets))
        self.assertTrue(any(r["marker"] == "1." for r in bullets))

    def test_headings_do_not_print_their_hash_markers(self):
        records = today_page._markdown_to_line_records("### Third Level Heading\n")
        heading = next(r for r in records if r["style"] == "h3")
        self.assertEqual("Third Level Heading", heading["text"])
        self.assertNotIn("#", heading["text"])

    def test_variable_height_pagination_fills_pages(self):
        records = [
            today_page._appendix_record(f"line {index}", "body")
            for index in range(400)
        ]
        pages = today_page._paginate_appendix_lines(records)
        self.assertGreater(len(pages), 1)
        flattened = [record for page in pages for record in page]
        self.assertEqual(records, flattened)
        content_span = today_page.APPENDIX_CONTENT_TOP - today_page.PAGE_CONTENT_BOTTOM
        for page in pages[:-1]:
            total_height = sum(record["height"] for record in page)
            self.assertLessEqual(total_height, content_span)

    def test_no_orphaned_heading_at_a_page_end(self):
        filler = [
            today_page._appendix_record(f"filler line {index}", "body")
            for index in range(60)
        ]
        heading = today_page._appendix_record("Trailing Heading", "h2")
        body_after = today_page._appendix_record("text right after", "body")
        records = filler + [heading, body_after]
        pages = today_page._paginate_appendix_lines(records)
        for page in pages:
            if page and page[-1]["style"] == "h2":
                self.fail("a heading was left as the last line on a page")
        for page_index, page in enumerate(pages):
            if heading in page:
                self.assertIn(body_after, page)

    def test_an_unreadable_reading_document_stops_the_build(self):
        """v2 printed an "attachment unavailable" page and shipped the card
        anyway. v3 refuses: a card without the document it is about is a card
        the reader cannot answer, so the row is held and named on the cover."""
        with tempfile.TemporaryDirectory() as directory:
            decision = {
                "item_id": "missing-md",
                "title": "Approve a file",
                "project": "test",
                "age": "2026-08-20",
                "ask": (
                    "One file may not exist. "
                    "Recommendation: check before approving. "
                    "Decide: (1:q1) [yes/no] Approve? "
                    "Row: missing_reading Read: nowhere.md"
                ),
            }
            with self.assertRaisesRegex(
                today_page.TodayPageError, "document to read"
            ):
                today_page.build_manifest(
                    "2026-08-20",
                    [],
                    [decision],
                    {},
                    attachment_root=Path(directory),
                )

    def test_curly_quotes_and_dashes_transliterate_instead_of_blanking(self):
        text = "He said ‘hi’ — an em dash … done."
        cleaned = today_page._transliterate_typography(text)
        self.assertTrue(all(ord(character) <= 127 for character in cleaned))
        self.assertIn("'hi'", cleaned)
        self.assertIn("--", cleaned)
        self.assertIn("...", cleaned)


class ReadingPageMarkdownLayoutTests(unittest.TestCase):
    """mc-dsb68: reading pages lay Markdown out, they do not print its source.

    The 2026-09-03 second reader found pipe tables printed as monospace lines
    of literal pipes and backticks, rows broken mid-word across page ends,
    links printed with their brackets, and quotes shown as a bare bar. The
    fixtures carry the real passages from the two research documents that
    produced those pages.
    """

    FIXTURES = ROOT / "tests" / "fixtures" / "remarkable"

    def records(self, name):
        text = today_page._transliterate_typography(
            (self.FIXTURES / name).read_text(encoding="utf-8")
        )
        return today_page._markdown_to_line_records(text)

    def table_rows(self, records):
        return [record for record in records if record.get("cells") is not None]

    def test_a_pipe_table_becomes_columns_not_a_line_of_pipes(self):
        records = self.records("reading-anydoc-tables.md")
        rows = self.table_rows(records)
        self.assertGreaterEqual(len(rows), 16)

        headers = [row for row in rows if row["header"]]
        self.assertEqual(2, len(headers))
        for header in headers:
            font_name, _ = today_page._APPENDIX_STYLE_FONTS[header["style"]]
            self.assertEqual("Helvetica-Bold", font_name)
        self.assertEqual(
            ["Fact", "Value", "How verified"],
            [lines[0] for lines in headers[0]["cells"]],
        )

        for row in rows:
            for cell_lines in row["cells"]:
                for line in cell_lines:
                    self.assertNotIn("|", line)
                    self.assertNotIn("`", line)
                    self.assertNotIn("**", line)

    def test_table_cells_wrap_inside_their_own_column(self):
        rows = self.table_rows(self.records("reading-anydoc-tables.md"))
        available = today_page.PAGE_W - 2 * today_page.PAGE_MARGIN_X
        for row in rows:
            widths = [column["width"] for column in row["columns"]]
            gaps = today_page.APPENDIX_TABLE_COL_GAP * (len(widths) - 1)
            self.assertLessEqual(sum(widths) + gaps, available + 0.01)
            last = row["columns"][-1]
            self.assertLessEqual(
                today_page.PAGE_MARGIN_X + last["x"] + last["width"],
                today_page.PAGE_W - today_page.PAGE_MARGIN_X + 0.01,
            )
            font_name, font_size = today_page._APPENDIX_STYLE_FONTS[row["style"]]
            for index, cell_lines in enumerate(row["cells"]):
                for line in cell_lines:
                    self.assertLessEqual(
                        today_page._string_width(line, font_name, font_size),
                        widths[index] + 0.01,
                    )

    def test_rows_are_separated_by_a_rule_except_the_last(self):
        rows = self.table_rows(self.records("reading-cumora-quotes.md"))
        self.assertTrue(all(row["rule"] for row in rows[:-1]))
        self.assertFalse(rows[-1]["rule"])

    def test_a_table_too_wide_to_fit_becomes_labelled_blocks(self):
        records = self.records("reading-cumora-quotes.md")
        labelled = [
            record
            for record in records
            if record["style"] == "table"
            and record.get("cells") is None
            and record["text"].startswith("SandboxedEngineIdentifier:")
        ]
        self.assertEqual(2, len(labelled))
        groups = {record["group"] for record in labelled}
        self.assertEqual(2, len(groups))

        block = [record for record in records if record.get("group") == sorted(groups)[0]]
        self.assertEqual(
            [
                "SandboxedEngineIdentifier: ClaudeSandboxedAdapterIdentifier",
                "FilesystemBoundaryDescription: FailClosedFilesystemBoundaryEnforced",
                "ToolNetworkBoundaryDescription: FailClosedToolNetworkBoundaryEnforced",
                "HostExecutionFallbackDescription: HostExecutionFallbackNeverSelected",
                "OperatorAcceptanceRequirement: ExplicitOperatorAcceptanceRequired",
                "VerificationCommandInvocation: VerificationCommandInvocationRecorded",
                "ObservedRegressionWindow: ObservedRegressionWindowUnknown",
            ],
            [record["text"] for record in block],
        )
        self.assertTrue(all(record["indent"] == 1 for record in block))

    def test_a_table_row_never_straddles_a_page_boundary(self):
        """Slide the whole reading down one filler line at a time: whatever
        lands on the boundary, no row and no labelled block may be split."""
        for name in ("reading-anydoc-tables.md", "reading-cumora-quotes.md"):
            base = self.records(name)
            for offset in range(0, 46, 3):
                filler = [
                    today_page._appendix_record(f"filler {index}", "body")
                    for index in range(offset)
                ]
                pages = today_page._paginate_appendix_lines(filler + base)
                placed = {}
                for page_index, page in enumerate(pages):
                    for record in page:
                        group = record.get("group")
                        if not group:
                            continue
                        placed.setdefault(group, set()).add(page_index)
                with self.subTest(fixture=name, offset=offset):
                    for group, page_indexes in placed.items():
                        self.assertEqual(
                            1,
                            len(page_indexes),
                            f"{group} was split across pages {sorted(page_indexes)}",
                        )
                    content_span = (
                        today_page.APPENDIX_CONTENT_TOP - today_page.PAGE_CONTENT_BOTTOM
                    )
                    for page in pages[:-1]:
                        if len(page) == 1:
                            continue
                        self.assertLessEqual(
                            sum(record["height"] for record in page), content_span
                        )


    def test_a_link_inside_a_table_cell_keeps_its_url(self):
        # The cross-reviewer's review finding on mc-dsb68: a link in a cell kept the label
        # and dropped the URL. In a cell the URL follows the label in the same
        # cell, since a cell has no room for an indented line of its own.
        matrix = today_page._table_matrix(
            [
                "| Item | Source |",
                "|---|---|",
                "| Example | [issue 94](https://example.test/issues/94) |",
            ]
        )
        self.assertEqual(
            [["Item", "Source"], ["Example", "issue 94 https://example.test/issues/94"]],
            matrix,
        )


    def test_a_pipe_inside_a_code_span_is_not_a_table_row(self):
        # Fourth read of 2026-09-03: a sentence with `curl ... | sh` in it was
        # detected as a two-column table and printed shredded.
        self.assertFalse(
            today_page._is_table_row(
                "Both skills tell the agent to install the binary with `curl ... | sh` if moli is not on PATH."
            )
        )
        self.assertTrue(today_page._is_table_row("| Item | Source |"))
        self.assertTrue(today_page._is_table_row("Item | `a | b` | Source"))
        # The cross-reviewer's review: a real row whose cell holds a code-span pipe must
        # keep that pipe inside the cell through the matrix split.
        matrix = today_page._table_matrix(
            [
                "| Item | Command | Source |",
                "|---|---|---|",
                "| Install | `curl ... | sh` | README |",
            ]
        )
        self.assertEqual(
            [["Item", "Command", "Source"], ["Install", "curl ... | sh", "README"]],
            matrix,
        )

    def test_inline_code_and_emphasis_markers_are_stripped(self):
        records = self.records("reading-anydoc-tables.md")
        body = " ".join(
            record["text"] for record in records if record["style"] == "body"
        )
        for marker in ("`", "**", "__"):
            self.assertNotIn(marker, body)
        self.assertNotIn("*3,588 ms*", body)
        self.assertIn("Result", body)
        self.assertIn("src/package/limits.rs", body)
        self.assertIn("97 ms", body)
        self.assertIn("3,588 ms", body)
        # Emphasis underscores go; an identifier's underscores stay.
        self.assertIn("close_ticket", body)
        self.assertIn("this phrase", body)
        self.assertNotIn("_this phrase_", body)
        self.assertIn("that phrase", body)

    def test_a_link_prints_its_text_then_its_url_on_an_indented_line(self):
        records = self.records("reading-anydoc-tables.md")
        body = [record for record in records if record["style"] == "body"]
        joined = " ".join(record["text"] for record in body)
        self.assertIn("#94", joined)
        self.assertNotIn("[#94]", joined)
        self.assertNotIn("(https://github.com/firecrawl/anydoc/issues/94)", joined)

        links = [record for record in records if record["style"] == "link"]
        urls = [record["text"] for record in links]
        self.assertIn("https://github.com/firecrawl/anydoc/issues/94", urls)
        self.assertIn("https://github.com/firecrawl/anydoc/issues/157", urls)
        self.assertIn("https://github.com/firecrawl/anydoc/issues/146", urls)
        # A link the source wrapped across a line break is still a link.
        self.assertNotIn("[#156", joined)
        self.assertIn("https://github.com/firecrawl/anydoc/issues/156", urls)
        for record in links:
            self.assertEqual(1, record["indent"])
            self.assertIsNone(record["marker"])
            self.assertNotIn("(", record["text"])
            self.assertNotIn(")", record["text"])
            self.assertNotIn("[", record["text"])

    def test_a_bare_url_is_left_exactly_as_written(self):
        records = self.records("reading-anydoc-tables.md")
        body = " ".join(
            record["text"] for record in records if record["style"] == "body"
        )
        self.assertIn("https://github.com/firecrawl/anydoc is written bare", body)

    def test_a_block_quote_is_a_labelled_indented_paragraph_not_a_bar(self):
        records = self.records("reading-cumora-quotes.md")
        labels = [record for record in records if record["style"] == "quote_label"]
        self.assertEqual(1, len(labels))
        self.assertEqual("Quote:", labels[0]["text"])
        self.assertEqual(1, labels[0]["indent"])

        quote = [record for record in records if record["style"] == "quote"]
        self.assertTrue(quote)
        for record in quote:
            self.assertEqual(1, record["indent"])
            self.assertIsNone(record["marker"])
            self.assertNotIn(">", record["text"])
        joined = " ".join(record["text"] for record in quote)
        self.assertIn("fail-closed filesystem + tool-network boundary", joined)
        # The label and the quote's first line move together.
        self.assertEqual(labels[0]["group"], quote[0]["group"])
        self.assertIsNotNone(labels[0]["group"])

    def test_the_fixtures_render_to_a_pdf_end_to_end(self):
        """The records are only half the claim: the reading has to reach a
        real PDF page without the drawing code choking on a table row."""
        decisions = []
        for index, fixture in enumerate(
            ("reading-anydoc-tables.md", "reading-cumora-quotes.md"), start=1
        ):
            decisions.append(
                {
                    "item_id": f"markdown-layout-{index}",
                    "title": f"Approve the markdown layout sample {index}",
                    "project": "example-project",
                    "age": "2026-09-03",
                    "ask": (
                        "The reading behind this card is a layout fixture. "
                        "Recommendation: approve it. "
                        f"Decide: (1:q1) [yes/no] Approve sample {index}? "
                        f"Row: layout_{index} "
                        f"Read: tests/fixtures/remarkable/{fixture}"
                    ),
                }
            )
        manifest = today_page.build_manifest(
            "2026-09-03", [], decisions, {"queue": "markdown-layout"}
        )
        today_page.validate_manifest(manifest)
        appendix_rows = [
            record
            for appendix in manifest["appendices"]
            for page in appendix["pages"]
            for record in page
            if isinstance(record, dict) and record.get("cells") is not None
        ]
        self.assertTrue(appendix_rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.pdf"
            today_page.render_pdf(path, manifest)
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 1000)


class PenAnswerGeometryTests(unittest.TestCase):
    """The measured layout: 24-point boxes on a 44-point minimum row pitch."""

    def setUp(self):
        self.manifest = today_page.build_manifest(
            "2026-08-20", [], _sample_decisions(9, 3), {"queue": "geometry"}
        )

    def boxes_by_entry(self, manifest):
        return {
            entry["entry_id"]: {
                box["choice"]: box["bbox"] for box in entry["boxes"]
            }
            for entry in manifest["entries"]
            if entry["kind"] == "decision"
        }

    def test_answer_geometry_matches_the_measured_pen_spec(self):
        self.assertEqual(24.0, today_page.BOX_SIZE)
        self.assertEqual(44.0, today_page.MIN_ROW_PITCH)
        self.assertEqual(4.0, today_page.BOX_TOLERANCE)
        # The v3 pitch has to leave the resolver's tolerance margin between two
        # boxes, or one tick could be matched to two answers.
        self.assertGreater(
            today_page.ANSWER_BOX_PITCH,
            today_page.BOX_SIZE + 2 * today_page.BOX_TOLERANCE,
        )
        boxes = self.boxes_by_entry(self.manifest)
        self.assertTrue(boxes)
        for entry_id, choices in boxes.items():
            with self.subTest(entry_id=entry_id):
                for choice, bbox in choices.items():
                    self.assertAlmostEqual(
                        today_page.BOX_SIZE, bbox[2] - bbox[0], places=2
                    )
                    self.assertAlmostEqual(
                        today_page.BOX_SIZE, bbox[3] - bbox[1], places=2
                    )
                yes = (choices["YES"][0] + choices["YES"][2]) / 2
                no = (choices["NO"][0] + choices["NO"][2]) / 2
                self.assertAlmostEqual(
                    today_page.ANSWER_BOX_PITCH, no - yes, places=2
                )

    def test_every_question_carries_defer_and_discard(self):
        """The reader 2026-09-01: every question, whatever its form, can be deferred
        or discarded. Nothing else in the manifest guarantees this per entry."""
        entries = [
            entry
            for entry in self.manifest["entries"]
            if entry["kind"] == "decision"
        ]
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(entry=entry["entry_id"]):
                choices = [box["choice"] for box in entry["boxes"]]
                self.assertIn("DEFER", choices)
                self.assertIn("DISCARD", choices)
                self.assertEqual(["DEFER", "DISCARD"], choices[-2:])

    def test_adjacent_rows_are_at_least_the_minimum_pitch_apart(self):
        centres_by_card = {}
        for entry in self.manifest["entries"]:
            if entry["kind"] != "decision":
                continue
            first = entry["boxes"][0]["bbox"]
            centres_by_card.setdefault(
                (entry["card_id"], entry["page"]), []
            ).append((first[1] + first[3]) / 2)
        self.assertTrue(centres_by_card)
        for key, centres in centres_by_card.items():
            for first, second in zip(centres, centres[1:]):
                with self.subTest(card=key):
                    self.assertGreaterEqual(
                        round(first - second, 6), today_page.MIN_ROW_PITCH
                    )

    def test_a_worst_case_measured_tick_touches_only_its_own_box(self):
        """A real tick, drawn as badly as the reader has ever drawn one, hits one box.

        The old 13-point box on a 25-point pitch could not contain a mark this
        size: a tick of the measured height, offset by the largest observed
        centre error, reached into the vertically adjacent question's tolerance
        margin and the mark was quarantined instead of counted.
        """
        boxes = [
            (entry, box)
            for entry in self.manifest["entries"]
            for box in entry["boxes"]
        ]
        for entry, box in boxes:
            cx = (box["bbox"][0] + box["bbox"][2]) / 2
            cy = (box["bbox"][1] + box["bbox"][3]) / 2
            for dx in (-MEASURED_MAX_CENTRE_OFFSET_X, MEASURED_MAX_CENTRE_OFFSET_X):
                for dy in (
                    -MEASURED_MAX_CENTRE_OFFSET_Y,
                    MEASURED_MAX_CENTRE_OFFSET_Y,
                ):
                    mark = [
                        cx + dx - MEASURED_TICK_WIDTH / 2,
                        cy + dy - MEASURED_TICK_HEIGHT / 2,
                        cx + dx + MEASURED_TICK_WIDTH / 2,
                        cy + dy + MEASURED_TICK_HEIGHT / 2,
                    ]
                    touched = [
                        (other["entry_id"], other_box["choice"])
                        for other, other_box in boxes
                        if other["page"] == entry["page"]
                        and today_page._intersects(
                            mark, today_page._expanded(other_box["bbox"])
                        )
                    ]
                    with self.subTest(entry=entry["entry_id"], dx=dx, dy=dy):
                        self.assertEqual(
                            [(entry["entry_id"], box["choice"])], touched
                        )

    def test_generated_hit_regions_never_overlap_on_a_page(self):
        by_page = {}
        for entry in self.manifest["entries"]:
            for box in entry["boxes"]:
                by_page.setdefault(entry["page"], []).append(
                    (entry["entry_id"], box["choice"], box["bbox"])
                )
        for page, boxes in by_page.items():
            for index, (entry_id, choice, bbox) in enumerate(boxes):
                for other_id, other_choice, other in boxes[index + 1 :]:
                    with self.subTest(page=page, a=entry_id, b=other_id):
                        self.assertFalse(
                            today_page._intersects(
                                today_page._expanded(bbox),
                                today_page._expanded(other),
                            ),
                            f"{entry_id}/{choice} overlaps "
                            f"{other_id}/{other_choice}",
                        )

    def test_no_card_or_answer_box_crosses_a_page_boundary(self):
        manifest = today_page.build_manifest(
            "2026-08-20", [], _sample_decisions(24, 3), {"queue": "bounds"}
        )
        fragments = {
            fragment["page"]: fragment
            for card in manifest["cards"]
            for fragment in card["fragments"]
        }
        for fragment in fragments.values():
            self.assertGreaterEqual(
                fragment["bottom"], today_page.PAGE_CONTENT_BOTTOM
            )
            self.assertLessEqual(fragment["top"], today_page.PAGE_CONTENT_TOP)
        for entry in manifest["entries"]:
            fragment = fragments[entry["page"]]
            for box in entry["boxes"]:
                x0, y0, x1, y1 = today_page._expanded(box["bbox"])
                with self.subTest(entry=entry["entry_id"]):
                    self.assertGreaterEqual(x0, fragment["x"])
                    self.assertLessEqual(x1, fragment["x"] + fragment["width"])
                    self.assertGreaterEqual(y0, fragment["bottom"])
                    self.assertLessEqual(y1, fragment["top"])

    def test_page_bounds_guard_rejects_a_box_outside_its_card(self):
        entries = copy.deepcopy(
            [e for e in self.manifest["entries"] if e["kind"] == "decision"]
        )
        fragments = {
            fragment["page"]: fragment
            for card in self.manifest["cards"]
            for fragment in card["fragments"]
        }
        entries[0]["boxes"][0]["bbox"] = [10.0, 40.0, 34.0, 64.0]
        with self.assertRaisesRegex(
            today_page.TodayPageError, "outside its own card"
        ):
            today_page._assert_v3_bounds(entries, fragments)

    def test_separation_guard_rejects_two_boxes_that_share_a_hit_region(self):
        """The false-mark failure, caught before a document is written."""
        entries = copy.deepcopy(
            [e for e in self.manifest["entries"] if e["kind"] == "decision"]
        )
        first = entries[0]["boxes"][0]["bbox"]
        entries[0]["boxes"][1]["bbox"] = [
            first[0] + 2.0,
            first[1],
            first[2] + 2.0,
            first[3],
        ]
        with self.assertRaisesRegex(
            today_page.TodayPageError, "read as two answers"
        ):
            today_page._assert_box_separation(entries)


# ---------------------------------------------------------------------------
# mc-rrjv work item 2: every card names the system it is about
# ---------------------------------------------------------------------------


class CardSystemLabelTests(unittest.TestCase):
    """The reader, on the 2026-08-16 sheet: label each card with its system."""

    def render(self, manifest):
        """The PDF's own drawn text, decompressed - not just the manifest.

        The reader reads the page, not the JSON. A label recorded in the manifest and
        never drawn would be a label he never sees.
        """
        import base64
        import re as _re
        import zlib

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.pdf"
            today_page.render_pdf(path, manifest)
            raw = path.read_bytes()
        drawn = b""
        for chunk in _re.findall(rb"stream\r?\n(.*?)endstream", raw, _re.S):
            payload = chunk.strip()
            for decode in (
                lambda value: zlib.decompress(
                    base64.a85decode(value, adobe=True)
                ),
                zlib.decompress,
                lambda value: value,
            ):
                try:
                    drawn += decode(payload)
                    break
                except Exception:
                    continue
        return drawn

    def test_a_row_with_a_project_renders_it_as_the_card_system(self):
        manifest = today_page.build_manifest(
            "2026-08-20",
            [],
            _sample_decisions(1, 1, project="field-app"),
            {"queue": "label"},
        )
        card = manifest["cards"][0]
        self.assertEqual("field-app", card["system"])
        self.assertEqual("field-app", card["system"])
        self.assertEqual(
            "system",
            card["fragments"][0]["blocks"][0]["kind"],
        )
        self.assertIn(b"System: field-app", self.render(manifest))

    def test_a_row_with_no_project_or_ticket_renders_cleanly(self):
        manifest = today_page.build_manifest(
            "2026-08-20",
            [],
            _sample_decisions(1, 1, project=""),
            {"queue": "label"},
        )
        card = manifest["cards"][0]
        self.assertEqual("", card["system"])
        blocks = card["fragments"][0]["blocks"]
        self.assertNotIn("system", [block["kind"] for block in blocks])
        self.assertNotIn(b"System:", self.render(manifest))
        # with no system line, the card opens with its own question, at the
        # top of the card
        self.assertEqual("headline", blocks[0]["kind"])
        self.assertEqual(
            blocks[0]["y_top"],
            round(card["fragments"][0]["top"] - today_page.CARD_PADDING, 2),
        )

    def test_the_system_label_falls_back_to_the_owning_beads_project(self):
        decision = {
            "project": "",
            "title": "Deploy the access-log credential redaction (mc-4tyq)",
            "ask": "",
        }
        self.assertEqual(
            "example-project",
            today_page.card_system_label(decision, {"mc": "example-project"}),
        )

    def test_an_unknown_ticket_prefix_prints_nothing_rather_than_guessing(self):
        decision = {
            "project": "",
            "title": "Do the thing (zz-9plural)",
            "ask": "",
        }
        self.assertEqual(
            "", today_page.card_system_label(decision, {"mc": "example-project"})
        )

# ---------------------------------------------------------------------------
# mc-rrjv work item 3: a question with a recorded answer is never re-asked
# ---------------------------------------------------------------------------


ANSWERED_QUESTION = "Clear the old test copies?"
UNANSWERED_QUESTION = "Approve the never-before-asked sample?"


class AlreadyAnsweredGuardTests(unittest.TestCase):
    """The 2026-08-19 failure, made mechanically impossible."""

    def queue_text(self, questions, pen_section=True):
        rows = "".join(
            f"| Choose this {index} | example-project | 2026-08-20 | "
            "It is ready. It needs an answer. Recommendation: approve it. "
            f"Decide: (1:q1) [yes/no] {question} Row: row_{index} Read: tests/fixtures/remarkable/reading-sample.md |\n"
            for index, question in enumerate(questions)
        )
        text = (
            "# Queue\n\n## Active - First\n\n"
            "| Item | Project | Age | Ask |\n|---|---|---|---|\n" + rows
        )
        if pen_section:
            text += (
                "\n## Pen dispositions - 2026-08-16 (applied by consume_decisions)\n\n"
                f"- **YES** {ANSWERED_QUESTION} - row: Free up space\n"
            )
        return text

    def run_generate(self, questions, archive, pen_section=True):
        original = today_page.render_pdf
        today_page.render_pdf = lambda path, manifest: path.write_bytes(b"pdf")
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                queue = base / "REVIEW-QUEUE.md"
                report = base / "verify-report.json"
                queue.write_text(self.queue_text(questions, pen_section))
                report.write_text(
                    json.dumps(
                        {
                            "verified_at": "2026-08-20T06:00:00-07:00",
                            "rows": [
                                {"title": f"Choose this {index}", "ok": True}
                                for index in range(len(questions))
                            ],
                            "held": [],
                        }
                    )
                )
                _pdf, manifest_path = today_page.generate(
                    queue,
                    None,
                    report,
                    base / "out",
                    "2026-08-20",
                    state_path=base / "question-state.json",
                    answer_record_root=archive,
                )
                return json.loads(manifest_path.read_text())
        finally:
            today_page.render_pdf = original

    def archive(self, directory, payload="valid"):
        root = Path(directory) / "today-pages"
        processed = root / "2026-08-16" / "processed"
        processed.mkdir(parents=True)
        target = processed / "resolution.json"
        if payload == "valid":
            target.write_text(
                json.dumps(
                    {
                        "page_id": "decisions-2026-08-16-fixture",
                        "decisions": [
                            {
                                "entry_id": "decision-001a",
                                "choice": "YES",
                                "label": ANSWERED_QUESTION,
                            }
                        ],
                    }
                )
            )
        elif payload == "v3":
            target.write_text(
                json.dumps(
                    {
                        "page_id": "decisions-2026-08-16-fixture",
                        "layout_version": "decisions-v3.0",
                        "decisions": [
                            {
                                "entry_id": "decision-001a",
                                "question_key": "first",
                                "choice": "DEFER",
                                "label": ANSWERED_QUESTION,
                            },
                            {
                                "entry_id": "decision-001b",
                                "question_key": "when",
                                "choice": None,
                                "label": UNANSWERED_QUESTION,
                            },
                        ],
                    }
                )
            )
        elif payload == "corrupt":
            target.write_text('{"decisions": [ this is not json')
        else:
            target.write_text(json.dumps({"decisions": "not a list"}))
        return root

    def test_a_question_already_answered_on_aug_16_is_excluded_and_named(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.run_generate(
                [ANSWERED_QUESTION, UNANSWERED_QUESTION],
                self.archive(directory),
                pen_section=False,
            )
        excluded = manifest["excluded_already_answered"]
        self.assertEqual(1, len(excluded))
        self.assertEqual(ANSWERED_QUESTION, excluded[0]["question"])
        self.assertEqual("2026-08-16", excluded[0]["answered_on"])
        self.assertEqual("YES", excluded[0]["choice"])
        self.assertEqual(1, manifest["entry_counts"]["excluded_already_answered"])
        printed = [entry["label"] for entry in manifest["entries"]]
        self.assertNotIn(ANSWERED_QUESTION, printed)
        self.assertIn(UNANSWERED_QUESTION, printed)

    def test_the_queue_pen_answer_sections_are_a_record_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "today-pages"
            root.mkdir(parents=True)
            manifest = self.run_generate(
                [ANSWERED_QUESTION, UNANSWERED_QUESTION], root
            )
        excluded = manifest["excluded_already_answered"]
        self.assertEqual(1, len(excluded))
        self.assertEqual("2026-08-16", excluded[0]["answered_on"])
        self.assertIn("REVIEW-QUEUE.md", excluded[0]["source"])

    def test_a_row_with_no_recorded_answer_prints_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.run_generate(
                [UNANSWERED_QUESTION], self.archive(directory), pen_section=False
            )
        self.assertEqual([], manifest["excluded_already_answered"])
        self.assertEqual(
            [UNANSWERED_QUESTION],
            [entry["label"] for entry in manifest["entries"]],
        )

    def test_a_v3_day_is_governed_by_the_state_file_not_this_record(self):
        # 2026-09-02 evening: the first v3 pen round left a resolution with a
        # DEFER box and a write-in (choice None). The legacy reader called the
        # write-in unreadable and would have counted the DEFER as an answer,
        # dropping a card the reader deferred. A v3 day is skipped here entirely.
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.run_generate(
                [ANSWERED_QUESTION, UNANSWERED_QUESTION],
                self.archive(directory, "v3"),
                pen_section=False,
            )
        self.assertEqual([], manifest["excluded_already_answered"])
        self.assertEqual(
            [ANSWERED_QUESTION, UNANSWERED_QUESTION],
            [entry["label"] for entry in manifest["entries"]],
        )

    def test_a_corrupted_answer_record_fails_the_run(self):
        for payload in ("corrupt", "wrong-shape"):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(today_page.AnswerRecordError):
                        self.run_generate(
                            [UNANSWERED_QUESTION],
                            self.archive(directory, payload),
                            pen_section=False,
                        )

    def test_a_missing_answer_record_fails_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                today_page.AnswerRecordError, "could be re-asked"
            ):
                self.run_generate(
                    [UNANSWERED_QUESTION],
                    Path(directory) / "not-here",
                    pen_section=False,
                )

    def test_an_undated_pen_section_fails_the_run(self):
        with self.assertRaisesRegex(
            today_page.AnswerRecordError, "carries no date"
        ):
            today_page.load_recorded_answers(
                "## Pen dispositions - recovered\n\n- **YES** Something?\n",
                today_page.ANSWER_RECORD_ROOT,
            )

    def test_an_appendix_reference_does_not_hide_a_recorded_answer(self):
        self.assertEqual(
            today_page.normalize_question("Approve the plan?"),
            today_page.normalize_question(
                "Approve the plan? Full text: Appendix B, page 4."
            ),
        )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# mc-iacbw: the v3 contract. Every question in the form it needs, Defer and
# Discard on all of them, the reading behind the card, nothing held silently,
# and a review that is bound to the exact artifact.
# ---------------------------------------------------------------------------

READING = "tests/fixtures/remarkable/reading-sample.md"


def _v3_row(ask_decide, title="Choose the sample", item="sample"):
    """One current-contract row: keyed questions, a row key, a reading."""
    return {
        "item_id": item,
        "title": title,
        "project": "test",
        "age": "2026-09-01",
        "ask": (
            "The sample is ready. It needs an answer. "
            "Recommendation: approve it. "
            f"Decide: {ask_decide} Row: {item} Read: {READING}"
        ),
    }


class AnswerFormTests(unittest.TestCase):
    """The reader 2026-09-01: the answer form fits the question, and the row says
    which form that is. Guessing it from the wording is what printed Y and N
    boxes under two open questions on the 2026-09-01 sheet."""

    def test_every_answer_form_is_accepted_and_shapes_its_boxes(self):
        cases = {
            "(1:a) [yes/no] Go ahead?": ("yes_no", ["YES", "NO"], False),
            "(1:a) [choice: A = first, B = second] Which one?": (
                "choice", ["A", "B"], True,
            ),
            "(1:a) [choice: A = one, B = two, C = three, D = four, E = five] "
            "Which one?": ("choice", ["A", "B", "C", "D", "E"], True),
            "(1:a) [write-in] Which day and hour?": ("write_in", [], True),
        }
        for decide, (answer_type, answers, writes_in) in cases.items():
            with self.subTest(decide=decide):
                manifest = today_page.build_manifest(
                    "2026-09-02", [], [_v3_row(decide)], {}
                )
                entry = manifest["entries"][0]
                self.assertEqual(answer_type, entry["answer_type"])
                self.assertEqual(
                    answers + ["DEFER", "DISCARD"],
                    [box["choice"] for box in entry["boxes"]],
                )
                self.assertEqual(writes_in, entry["write_in"] is not None)

    def test_a_question_with_no_answer_form_holds_the_row(self):
        row = (
            "| Choose the sample | test | 2026-09-01 | The sample is ready. "
            "It needs an answer. Recommendation: approve it. "
            f"Decide: (1:a) Approve the sample? Row: no_form_row Read: {READING} |"
        )
        reasons = today_page.validate_row(row)
        self.assertEqual(1, len(reasons))
        self.assertIn("does not say how it is answered", reasons[0])

    def test_a_question_with_no_key_holds_the_row(self):
        row = (
            "| Choose the sample | test | 2026-09-01 | The sample is ready. "
            "It needs an answer. Recommendation: approve it. "
            f"Decide: (1) [yes/no] Approve the sample? Row: no_key_row Read: {READING} |"
        )
        reasons = today_page.validate_row(row)
        self.assertEqual(1, len(reasons))
        self.assertIn("question key", reasons[0])

    def test_a_malformed_choice_holds_the_row(self):
        for decide, needle in (
            ("(1:a) [choice: A = only one] Which one?", "between 2 and 5"),
            (
                "(1:a) [choice: A = a, B = b, C = c, D = d, E = e, F = f] "
                "Which one?",
                "between 2 and 5",
            ),
            ("(1:a) [choice: A = a, C = c] Which one?", "letter its choices"),
            ("(1:a) [choice: first, second] Which one?", "equals sign"),
            ("(1:a) [maybe] Which one?", "does not print"),
        ):
            with self.subTest(decide=decide):
                with self.assertRaises(today_page.TodayPageError) as caught:
                    today_page._decision_copy(_v3_row(decide))
                self.assertIn(needle, str(caught.exception))

    def test_a_row_that_holds_does_not_crash_the_generator(self):
        """A contract failure has to hold the row and be reportable, never
        take the 06:00 run down with it (mc-sguf class)."""
        row = (
            "| Choose the sample | test | 2026-09-01 | The sample is ready. "
            "It needs an answer. Recommendation: approve it. "
            f"Decide: (1:a) Approve the sample? Row: no_form_row Read: {READING} |"
        )
        self.assertTrue(today_page.validate_row(row))
        text = (
            "# Queue\n\n## Active\n\n"
            "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
            + row
            + "\n"
        )
        with self.assertRaises(today_page.TodayPageError):
            today_page.parse_review_queue_text(text)

    def test_entry_ids_are_identical_across_a_rerender(self):
        row = _v3_row(
            "(1:one) [yes/no] Go ahead? Recommended: yes because it is ready. "
            "(2:two) [write-in] When? Recommended: this week because nothing blocks it."
        )
        first = today_page.build_manifest("2026-09-02", [], [row], {})
        second = today_page.build_manifest("2026-09-02", [], [row], {})
        self.assertEqual(
            [entry["entry_id"] for entry in first["entries"]],
            [entry["entry_id"] for entry in second["entries"]],
        )
        self.assertEqual(first["page_id"], second["page_id"])


class CoverPageTests(unittest.TestCase):
    """Contract section 5: nothing is withheld silently."""

    def render(self, manifest):
        """The text actually drawn on the page, not just the manifest."""
        import base64
        import re as _re
        import zlib

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cover.pdf"
            today_page.render_pdf(path, manifest)
            raw = path.read_bytes()
        drawn = b""
        for chunk in _re.findall(rb"stream\r?\n(.*?)endstream", raw, _re.S):
            payload = chunk.strip()
            for decode in (
                lambda value: zlib.decompress(
                    base64.a85decode(value, adobe=True)
                ),
                zlib.decompress,
                lambda value: value,
            ):
                try:
                    drawn += decode(payload)
                    break
                except Exception:
                    continue
        return drawn

    def test_a_held_row_is_named_on_the_cover_with_its_reason(self):
        held = [
            {
                "title": "A row that could not be printed",
                "reason": "the document this card is about could not be read",
            }
        ]
        manifest = today_page.build_manifest(
            "2026-09-02",
            [],
            [_v3_row("(1:a) [yes/no] Go ahead?")],
            {},
            held=held,
        )
        self.assertEqual(held, manifest["held"])
        self.assertEqual(1, manifest["entry_counts"]["held"])
        lines = [
            line
            for fragment in manifest["cover"]
            for block in fragment["blocks"]
            for line in block["lines"]
        ]
        cover = " ".join(lines)
        self.assertIn("A row that could not be printed", cover)
        self.assertIn(
            "Not ready for a decision yet, a document it points at could not "
            "be read", cover,
        )
        self.assertIn("1 card to answer", cover)
        self.assertIn(b"A row that could not be printed", self.render(manifest))

    def test_the_cover_says_so_when_nothing_is_held(self):
        manifest = today_page.build_manifest(
            "2026-09-02", [], [_v3_row("(1:a) [yes/no] Go ahead?")], {}, held=[]
        )
        cover = " ".join(
            line
            for fragment in manifest["cover"]
            for block in fragment["blocks"]
            for line in block["lines"]
        )
        self.assertNotIn("Every open item is in this document.", cover)
        self.assertIn("Parked items remain in the review queue.", cover)

    def test_the_verify_report_turns_a_held_row_into_plain_english(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "verify.json"
            report.write_text(
                json.dumps(
                    {
                        "verified_at": "2026-09-02T06:00:00-07:00",
                        "rows": [
                            {"title": "A good row", "ok": True, "reasons": []},
                            {
                                "title": "A held row",
                                "ok": False,
                                "reasons": [
                                    "card contract: Decide question 1 does not "
                                    "say how it is answered."
                                ],
                            },
                        ],
                    }
                )
            )
            loaded = today_page.load_verification_report(report)
        self.assertEqual(["A good row"], loaded["verified_titles"])
        self.assertEqual(1, len(loaded["held"]))
        self.assertEqual("A held row", loaded["held"][0]["title"])
        self.assertIn("wrong boxes", loaded["held"][0]["reason"])


class ReviewReceiptGateTests(unittest.TestCase):
    """Contract section 6: the review covers the exact artifact, or nothing
    is pushed. The cross-reviewer's finding 2: the rule was prose, and an edit after the
    review did not make the review stale."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "remarkable" / "review-receipt.py"
        spec = importlib.util.spec_from_file_location("review_receipt", path)
        cls.gate = importlib.util.module_from_spec(spec)
        sys.modules["review_receipt"] = cls.gate
        spec.loader.exec_module(cls.gate)

    def build(self, directory):
        manifest = today_page.build_manifest(
            "2026-09-02",
            [],
            [
                _v3_row("(1:a) [yes/no] Go ahead?", item="one"),
                _v3_row("(1:b) [write-in] When?", title="Choose when", item="two"),
            ],
            {},
        )
        pdf = Path(directory) / "decisions-2026-09-02.pdf"
        manifest_path = Path(directory) / "decisions-2026-09-02.manifest.json"
        today_page.render_pdf(pdf, manifest)
        manifest_path.write_text(json.dumps(manifest))
        return manifest, pdf, manifest_path

    def receipt(self, directory, manifest, pdf, verdicts, reviewer="a-reviewer"):
        path = Path(directory) / "review-receipt.json"
        manifest_path = Path(directory) / "decisions-2026-09-02.manifest.json"
        path.write_text(
            json.dumps(
                {
                    "pdf_sha256": self.gate.sha256_file(pdf),
                    "manifest_hash": manifest["manifest_hash"],
                    "manifest_sha256": self.gate.sha256_file(manifest_path),
                    "reviewer": reviewer,
                    "reviewed_at": "2026-09-02T04:30:00-07:00",
                    "cards": verdicts,
                }
            )
        )
        return path

    def all_yes(self, manifest, **overrides):
        cards = []
        for card in manifest["cards"]:
            verdict = {
                "card_id": card["card_id"],
                "complete_context": "yes",
                "forms_fit": "yes",
                "plain_english": "yes",
                "note": "",
            }
            verdict.update(overrides.get(card["card_id"], {}))
            cards.append(verdict)
        return cards

    def test_a_clean_receipt_lets_the_document_through(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            receipt = self.receipt(directory, manifest, pdf, self.all_yes(manifest))
            self.assertEqual(
                [], self.gate.check_receipt(receipt, pdf, manifest_path, "orchestrator")
            )

    def test_a_missing_receipt_stops_the_push(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            problems = self.gate.check_receipt(
                Path(directory) / "nothing.json", pdf, manifest_path, "orchestrator"
            )
            self.assertEqual(1, len(problems))
            self.assertIn("no review receipt", problems[0])

    def test_an_edit_after_the_review_invalidates_it(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            receipt = self.receipt(directory, manifest, pdf, self.all_yes(manifest))
            pdf.write_bytes(pdf.read_bytes() + b"%edited\n")
            problems = self.gate.check_receipt(
                receipt, pdf, manifest_path, "orchestrator"
            )
            self.assertTrue(
                any("changed since it was reviewed" in p for p in problems),
                problems,
            )

    def test_manifest_bytes_changed_without_changing_its_hash_field_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            receipt = self.receipt(directory, manifest, pdf, self.all_yes(manifest))
            changed = json.loads(manifest_path.read_text())
            changed["extra_unreviewed_field"] = "new content"
            manifest_path.write_text(json.dumps(changed))
            self.assertEqual(manifest["manifest_hash"], changed["manifest_hash"])
            problems = self.gate.check_receipt(
                receipt, pdf, manifest_path, "orchestrator"
            )
            self.assertTrue(
                any("manifest has changed since it was reviewed" in p for p in problems),
                problems,
            )

    def test_receipt_without_manifest_file_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            receipt = self.receipt(directory, manifest, pdf, self.all_yes(manifest))
            payload = json.loads(receipt.read_text())
            del payload["manifest_sha256"]
            receipt.write_text(json.dumps(payload))
            problems = self.gate.check_receipt(
                receipt, pdf, manifest_path, "orchestrator"
            )
            self.assertTrue(
                any("does not include a hash of the manifest file" in p for p in problems),
                problems,
            )

    def test_a_card_with_a_no_stops_the_push(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            failing = manifest["cards"][1]["card_id"]
            receipt = self.receipt(
                directory,
                manifest,
                pdf,
                self.all_yes(
                    manifest,
                    **{
                        failing: {
                            "plain_english": "no",
                            "note": "the second question is not plain English",
                        }
                    },
                ),
            )
            problems = self.gate.check_receipt(
                receipt, pdf, manifest_path, "orchestrator"
            )
            self.assertEqual(1, len(problems))
            self.assertIn("plain-English test", problems[0])
            self.assertIn("not plain English", problems[0])

    def test_an_unreviewed_card_stops_the_push(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            verdicts = self.all_yes(manifest)[:1]
            receipt = self.receipt(directory, manifest, pdf, verdicts)
            problems = self.gate.check_receipt(
                receipt, pdf, manifest_path, "orchestrator"
            )
            self.assertEqual(1, len(problems))
            self.assertIn("No card in the receipt reviewed", problems[0])

    def test_the_producer_cannot_review_its_own_document(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, pdf, manifest_path = self.build(directory)
            receipt = self.receipt(
                directory, manifest, pdf, self.all_yes(manifest), reviewer="orchestrator"
            )
            problems = self.gate.check_receipt(
                receipt, pdf, manifest_path, "orchestrator"
            )
            self.assertTrue(
                any("has to be someone else" in p for p in problems), problems
            )


class ReproducibleRenderTests(unittest.TestCase):
    """The receipt binds a reviewer's answers to the PDF's hash, so the same
    manifest has to render the same bytes. reportlab stamps the wall clock
    into a PDF by default, which invalidated a receipt that nothing had
    actually invalidated: the gate refused every push, the reviewed one
    included (found 2026-09-01 testing the gate end to end)."""

    def test_the_same_manifest_renders_the_same_bytes(self):
        manifest = today_page.build_manifest(
            "2026-09-02", [], [_v3_row("(1:a) [yes/no] Go ahead?")], {}
        )
        with tempfile.TemporaryDirectory() as directory:
            digests = []
            for name in ("first.pdf", "second.pdf"):
                path = Path(directory) / name
                today_page.render_pdf(path, manifest)
                digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(digests[0], digests[1])


class ReadingPageRenderingTests(unittest.TestCase):
    """mc-iacbw: the non-producer review of tomorrow's Decisions document
    found two rendering defects. A reading page header must never print the
    repository path (the reader must never see one), and a card's "Read ..., then
    answer." note must name the absolute page numbers of its own reading,
    not a count from wherever the note happens to sit -- a card that spills
    onto a second card page pushes its reading later, and a relative count
    then points at pages that are still the card, not the reading."""

    def render(self, manifest):
        """The text actually drawn on the page, not just the manifest."""
        import base64
        import re as _re
        import zlib

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.pdf"
            today_page.render_pdf(path, manifest)
            raw = path.read_bytes()
        drawn = b""
        for chunk in _re.findall(rb"stream\r?\n(.*?)endstream", raw, _re.S):
            payload = chunk.strip()
            for decode in (
                lambda value: zlib.decompress(
                    base64.a85decode(value, adobe=True)
                ),
                zlib.decompress,
                lambda value: value,
            ):
                try:
                    drawn += decode(payload)
                    break
                except Exception:
                    continue
        return drawn

    def test_reading_page_header_has_no_file_path(self):
        manifest = today_page.build_manifest(
            "2026-09-02", [], [_v3_row("(1:a) [yes/no] Go ahead?")], {}
        )
        card = manifest["cards"][0]
        # the manifest keeps the path -- only the printed page must not
        self.assertEqual(READING, card["read"]["path"])
        drawn = self.render(manifest)
        self.assertNotIn(READING.encode(), drawn)
        self.assertNotIn(b"reading-sample.md", drawn)
        first, last = card["read"]["pages"]
        self.assertEqual(1, last - first + 1)
        self.assertIn(b"page 1 of 1 of the reading", drawn)
        self.assertIn(f"Reading for: {card['headline']}".encode(), drawn)

    def test_a_card_that_spills_names_the_correct_absolute_reading_pages_on_both_pages(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "long-reading.md").write_text(
                "# Long reading\n\n"
                + "\n\n".join(
                    f"Paragraph {index} with some body text to fill lines."
                    for index in range(200)
                )
                + "\n"
            )
            # ten yes/no questions do not fit on one card page, so the card
            # itself spills onto a second page before its reading starts.
            questions = " ".join(
                f"({index + 1}:q{index + 1}) [yes/no] Approve step "
                f"{index + 1}? Recommended: yes because step {index + 1} is "
                "ready."
                for index in range(10)
            )
            decision = {
                "item_id": "spill-card",
                "title": "Choose the sample",
                "project": "test",
                "age": "2026-09-01",
                "ask": (
                    "The sample is ready. It needs several answers. "
                    "Recommendation: approve it. "
                    f"Decide: {questions} Row: spill_row "
                    "Read: long-reading.md"
                ),
            }
            manifest = today_page.build_manifest(
                "2026-09-02", [], [decision], {}, attachment_root=root
            )
            card = manifest["cards"][0]
            # the card itself must actually spill for this test to prove
            # anything: two card pages, then the reading after both of them
            self.assertEqual(2, len(card["page_numbers"]))
            first, last = card["read"]["pages"]
            self.assertGreater(first, card["page_numbers"][-1])
            # Wording changed 2026-09-13 (mc-9jd4t): the reading is optional
            # background, never a precondition. What this test proves is
            # unchanged: the note repeats on every page of a spilled card and
            # names absolute page numbers.
            note = f"Background, optional: pages {first} to {last}.".encode()
            drawn = self.render(manifest)
            # once on the card's first page, once on its continuation page
            self.assertEqual(2, drawn.count(note))
            self.assertNotIn(b"Read the next", drawn)

    def test_a_card_with_no_reading_prints_no_read_note(self):
        text = (
            "## Active\n"
            "| Old row | test | 2026-08-16 | It is ready. It needs an "
            "answer. Recommendation: do it. Decide: (1) Go? |\n"
        )
        rows = today_page.parse_review_queue_text(text, require_keys=False)
        manifest = today_page.build_manifest(
            "2026-08-16", [], rows, {}, require_keys=False
        )
        self.assertIsNone(manifest["cards"][0]["read"])
        drawn = self.render(manifest)
        self.assertNotIn(b"then answer.", drawn)


class LegacyRowCompatibilityTests(unittest.TestCase):
    """v3 is the contract for rows printed today. It is NOT the shape of the
    material already on disk: archived queue sections, archived card asks and
    the consumer's legacy path all carry bare "(1)" questions with no answer
    form and no reading document. Those readers pass require_keys=False and
    must keep working, or the change to the row syntax silently breaks every
    replay of the reader's existing ink."""

    LEGACY = (
        "| Old row | test | 2026-08-16 | It is ready. It needs an answer. "
        "Recommendation: do it. Decide: (1) Go? (2) Also go? |"
    )

    def test_the_legacy_version_constants_are_named_and_separate(self):
        self.assertEqual(1, today_page.LEGACY_SCHEMA_VERSION)
        self.assertEqual("decisions-v2.3", today_page.LEGACY_LAYOUT_VERSION)
        self.assertEqual(2, today_page.SCHEMA_VERSION)
        self.assertEqual("decisions-v3.0", today_page.LAYOUT_VERSION)
        self.assertIn(
            today_page.LEGACY_SCHEMA_VERSION, today_page.SUPPORTED_SCHEMA_VERSIONS
        )
        self.assertIn(
            today_page.LEGACY_LAYOUT_VERSION, today_page.SUPPORTED_LAYOUT_VERSIONS
        )

    def test_a_legacy_row_parses_when_keys_are_not_required(self):
        text = "## Active\n" + self.LEGACY + "\n"
        rows = today_page.parse_review_queue_text(text, require_keys=False)
        self.assertEqual(1, len(rows))
        self.assertEqual(
            ["q1", "q2"],
            [form["question_key"] for form in rows[0]["decision_forms"]],
        )
        # a question with no form tag meant YES and NO in v2, and still does
        self.assertEqual(
            ["yes_no", "yes_no"],
            [form["answer_type"] for form in rows[0]["decision_forms"]],
        )
        self.assertIsNone(rows[0]["read"])
        self.assertEqual([], today_page.validate_row(self.LEGACY, require_keys=False))

    def test_the_same_row_is_held_under_the_current_contract(self):
        text = "## Active\n" + self.LEGACY + "\n"
        with self.assertRaises(today_page.TodayPageError):
            today_page.parse_review_queue_text(text)
        reasons = today_page.validate_row(self.LEGACY)
        self.assertEqual(1, len(reasons))
        self.assertIn("question key", reasons[0])

    def test_a_legacy_row_still_renders_and_validates(self):
        rows = today_page.parse_review_queue_text(
            "## Active\n" + self.LEGACY + "\n", require_keys=False
        )
        manifest = today_page.build_manifest(
            "2026-08-16", [], rows, {"queue": "legacy"}, require_keys=False
        )
        today_page.validate_manifest(manifest)
        # it says plainly that it has no reading, rather than leaving the
        # field out and letting a reader assume one
        self.assertIsNone(manifest["cards"][0]["read"])
        self.assertEqual([], manifest["appendices"])
        for entry in manifest["entries"]:
            self.assertEqual(
                ["YES", "NO", "DEFER", "DISCARD"],
                [box["choice"] for box in entry["boxes"]],
            )

    def test_the_source_of_this_module_is_ascii_only(self):
        source = (
            ROOT / "remarkable" / "today_page.py"
        ).read_text(encoding="utf-8")
        offenders = [
            (number, line.strip()[:60])
            for number, line in enumerate(source.splitlines(), 1)
            if any(ord(character) > 127 for character in line)
        ]
        self.assertEqual([], offenders)


class QuestionStateFilterTests(unittest.TestCase):
    """Contract section 4: the generator reads the durable per-question state
    before it lays anything out. Answered and discarded are terminal;
    deferred, needs_clarification and unanswered all print again."""

    FOUR = (
        "| Four-question card | test | 2026-09-04 | It is ready. If yes, which "
        "audience? Recommendation: yes to both. Decide: (1:one) [yes/no] First? "
        "Recommended: yes because it is ready. "
        "(2:two) [yes/no] Second? Recommended: yes because it is ready. "
        "(3:three) [write-in] Third? Recommended: this week because it is ready. "
        "(4:four) [yes/no] Fourth? Recommended: yes because it is ready. "
        "Row: four_question_row Read: " + READING + " |"
    )
    ONE = (
        "| Fully settled card | test | 2026-09-01 | It is ready. It needs an "
        "answer. Recommendation: do it. Decide: (1:done) [yes/no] Only "
        "question? Row: settled_row Read: " + READING + " |"
    )

    def rows(self):
        return today_page.parse_review_queue_text(
            "## Active\n" + self.FOUR + "\n" + self.ONE + "\n"
        )

    def state_for(self, rows):
        first, second = rows
        return {
            "schema_version": 1,
            "entries": {
                today_page._entry_identity(first["item_id"], "one"): {
                    "state": "answered",
                    "since": "2026-09-04",
                    "source": "decisions-2026-09-04-fixture",
                    "detail": "answered",
                },
                today_page._entry_identity(first["item_id"], "two"): {
                    "state": "discarded",
                    "since": "2026-09-04",
                    "source": "decisions-2026-09-04-fixture",
                    "detail": "discarded",
                },
                today_page._entry_identity(first["item_id"], "three"): {
                    "state": "deferred",
                    "since": "2026-09-04",
                    "source": "decisions-2026-09-04-fixture",
                    "detail": "deferred",
                },
                today_page._entry_identity(first["item_id"], "four"): {
                    "state": "needs_clarification",
                    "since": "2026-09-04",
                    "source": "decisions-2026-09-04-fixture",
                    "detail": "needs clarification",
                },
                today_page._entry_identity(second["item_id"], "done"): {
                    "state": "answered",
                    "since": "2026-09-04",
                    "source": "decisions-2026-09-04-fixture",
                    "detail": "answered",
                },
            },
        }

    def history_for(self, rows):
        state_entries = self.state_for(rows)["entries"]
        return {
            today_page._entry_identity(row["item_id"], form["question_key"]): {
                "item_id": row["item_id"],
                "question_key": form["question_key"],
                "question": form["question"],
            }
            for row in rows
            for form in row["decision_forms"]
            if state_entries.get(
                today_page._entry_identity(row["item_id"], form["question_key"]),
                {},
            ).get("state")
            in today_page._SETTLED_STATES
        }

    def test_a_partially_settled_card_stops_before_stale_context_can_print(self):
        rows = self.rows()
        self.assertIn("If yes, which audience?", rows[0]["ask"])
        self.assertIn("Recommendation: yes to both.", rows[0]["ask"])
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            'Keep these question keys unchanged: three, four',
        ):
            today_page.filter_settled_questions(
                rows, self.state_for(rows), self.history_for(rows)
            )

    def test_generate_writes_no_document_for_a_partially_settled_card(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue = base / "queue.md"
            queue.write_text("## Active\n" + self.FOUR + "\n")
            row = today_page.parse_review_queue(queue)[0]
            report = base / "verify.json"
            report.write_text(
                json.dumps(
                    {
                        "verified_at": "2026-09-04T06:00:00-07:00",
                        "rows": [
                            {
                                "title": "Four-question card",
                                "ok": True,
                                "reasons": [],
                            }
                        ],
                    }
                )
            )
            state = base / "question-state.json"
            archive = base / "today-pages"
            manifest_dir = archive / "2026-09-04"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "decisions-2026-09-04.manifest.json").write_text(
                json.dumps(
                    {
                        "page_id": "decisions-2026-09-04-fixture",
                        "entries": [
                            {
                                "kind": "decision",
                                "entry_id": today_page._entry_identity(
                                    row["item_id"], "one"
                                ),
                                "item_id": row["item_id"],
                                "question_key": "one",
                                "label": "First?",
                            }
                        ],
                    }
                )
            )
            state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": {
                            today_page._entry_identity(row["item_id"], "one"): {
                                "state": "answered",
                                "since": "2026-09-04",
                                "source": "decisions-2026-09-04-fixture",
                                "detail": "answered in the proof fixture",
                            }
                        },
                    }
                )
            )

            with self.assertRaisesRegex(
                today_page.TodayPageError,
                '"Four-question card" still contains settled question\\(s\\): First\\?',
            ):
                today_page.generate(
                    queue,
                    None,
                    report,
                    base / "out",
                    "2026-09-04",
                    answer_record_root=archive,
                    state_path=state,
                )
            self.assertFalse((base / "out").exists())

    def test_a_fully_settled_card_is_recorded_without_printing(self):
        rows = self.rows()
        state = self.state_for(rows)
        kept, settled, excluded = today_page.filter_settled_questions(
            [rows[1]], state, self.history_for(rows)
        )
        manifest = today_page.build_manifest(
            "2026-09-02",
            [],
            kept,
            {"queue": "state"},
            settled=settled,
            excluded_already_answered=excluded,
        )
        today_page.validate_manifest(manifest)
        self.assertEqual([], manifest["cards"])
        self.assertEqual(
            [("Fully settled card", "every question answered or discarded")],
            [(row["title"], row["reason"]) for row in manifest["settled"]],
        )
        self.assertEqual([], manifest["held"])
        self.assertEqual(1, len(excluded))
        self.assertEqual("Only question?", excluded[0]["question"])
        self.assertEqual("ANSWERED", excluded[0]["choice"])
        self.assertEqual(excluded, manifest["excluded_already_answered"])

    def test_renumbering_cannot_reuse_a_settled_question_key(self):
        rewritten = today_page.parse_review_queue_text(
            "## Active\n"
            "| Four-question card | test | 2026-09-04 | The remaining choices "
            "need answers. Recommendation: answer each on its own terms. "
            "Decide: (1:one) [write-in] Third? Recommended: this week because it is ready. "
            "(2:two) [yes/no] Fourth? Recommended: yes because it is ready. "
            "Row: four_question_row Read: " + READING + " |\n"
        )
        original = self.rows()
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "reuses settled question key 'one'.*First\\?.*Third\\?",
        ):
            today_page.filter_settled_questions(
                rewritten,
                self.state_for(original),
                self.history_for(original),
            )

    def test_a_rewritten_row_with_only_open_questions_can_render(self):
        rewritten = today_page.parse_review_queue_text(
            "## Active\n"
            "| Four-question card | test | 2026-09-01 | The remaining choices "
            "need answers. Recommendation: answer each on its own terms. "
            "Decide: (1:three) [write-in] Third? Recommended: this week because it is ready. "
            "(2:four) [yes/no] Fourth? Recommended: yes because it is ready. "
            "Row: four_question_row Read: " + READING + " |\n"
        )
        original = self.rows()
        state = self.state_for(original)
        kept, settled, excluded = today_page.filter_settled_questions(
            rewritten, state, self.history_for(original)
        )
        manifest = today_page.build_manifest(
            "2026-09-02",
            [],
            kept,
            {"queue": "state"},
            settled=settled,
            excluded_already_answered=excluded,
        )
        self.assertEqual([], settled)
        self.assertEqual(
            [("First?", "ANSWERED"), ("Second?", "DISCARDED")],
            [(row["question"], row["choice"]) for row in excluded],
        )
        self.assertEqual(
            ["three", "four"],
            [entry["question_key"] for entry in manifest["entries"]],
        )
        card = manifest["cards"][0]
        self.assertEqual(["three", "four"], card["question_keys"])
        self.assertEqual(["three", "four"], card["printed_question_keys"])
        self.assertNotIn("First?", card["ask"])
        self.assertNotIn("Second?", card["ask"])

    def test_the_hash_is_the_pinned_bytes(self):
        keys = ["one", "two", "three", "four"]
        expected = hashlib.sha256(
            "\0".join(sorted(keys)).encode("ascii")
        ).hexdigest()
        manifest = today_page.build_manifest(
            "2026-09-02", [], self.rows(), {"queue": "state"}
        )
        self.assertEqual(expected, manifest["cards"][0]["question_set_hash"])
        self.assertEqual(64, len(expected))
        first = self.rows()[0]
        self.assertEqual(
            hashlib.sha256(
                (first["item_id"] + "\0" + "one").encode("ascii")
            ).hexdigest()[:12],
            manifest["entries"][0]["entry_id"],
        )

    def test_an_empty_state_prints_everything(self):
        rows = self.rows()
        kept, settled, excluded = today_page.filter_settled_questions(
            rows,
            {"schema_version": 1, "entries": {}},
            {},
        )
        self.assertEqual([], settled)
        self.assertEqual([], excluded)
        manifest = today_page.build_manifest(
            "2026-09-02", [], kept, {"queue": "state"}
        )
        self.assertEqual(
            ["one", "two", "three", "four", "done"],
            [entry["question_key"] for entry in manifest["entries"]],
        )

    def test_an_unreadable_state_file_stops_the_run(self):
        """Fail closed: printing while blind to what he has already settled
        is the failure the whole chain exists to prevent."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue = base / "queue.md"
            queue.write_text("## Active\n" + self.FOUR + "\n")
            report = base / "verify.json"
            report.write_text(
                json.dumps(
                    {
                        "verified_at": "2026-09-02T06:00:00-07:00",
                        "rows": [
                            {
                                "title": "Four-question card",
                                "ok": True,
                                "reasons": [],
                            }
                        ],
                    }
                )
            )
            state = base / "question-state.json"
            state.write_text("{not json")
            with self.assertRaisesRegex(
                today_page.TodayPageError, "already answered could not be read"
            ):
                today_page.generate(
                    queue,
                    None,
                    report,
                    base / "out",
                    "2026-09-02",
                    state_path=state,
                )
            self.assertFalse((base / "out").exists())


class RowKeyIdentityTests(unittest.TestCase):
    """Contract sections 1 and 2: the row declares its own key and nothing
    else feeds identity. Until 2026-09-01 item_id came from the title,
    project and age, so editing a title changed every entry id under the row
    and would have resurrected a question the reader had discarded."""

    ROW = (
        "| A card title | test | 2026-09-01 | It is ready. It needs an answer. "
        "Recommendation: do it. Decide: (1:one) [yes/no] First? "
        "Recommended: yes because it is ready. "
        "(2:two) [write-in] Second? Recommended: this week because it is ready. "
        "Row: startup_switch Read: " + READING + " |"
    )

    def manifest_for(self, row, date="2026-09-02"):
        rows = today_page.parse_review_queue_text("## Active\n" + row + "\n")
        return rows, today_page.build_manifest(date, [], rows, {"queue": "key"})

    def test_the_item_id_is_the_pinned_bytes_of_the_row_key(self):
        rows, manifest = self.manifest_for(self.ROW)
        self.assertEqual("startup_switch", rows[0]["row_key"])
        self.assertEqual(
            hashlib.sha256("startup_switch".encode("ascii")).hexdigest()[:12],
            rows[0]["item_id"],
        )
        self.assertEqual(rows[0]["item_id"], manifest["cards"][0]["item_id"])

    def test_editing_the_title_changes_no_identity(self):
        rows, first = self.manifest_for(self.ROW)
        edited = self.ROW.replace(
            "A card title", "A completely different headline, reworded"
        ).replace("2026-09-01", "2026-09-02")
        rewritten, second = self.manifest_for(edited, date="2026-09-03")
        self.assertNotEqual(rows[0]["title"], rewritten[0]["title"])
        self.assertEqual(rows[0]["item_id"], rewritten[0]["item_id"])
        self.assertEqual(
            [entry["entry_id"] for entry in first["entries"]],
            [entry["entry_id"] for entry in second["entries"]],
        )
        self.assertEqual(
            first["cards"][0]["question_set_hash"],
            second["cards"][0]["question_set_hash"],
        )

    def test_a_row_with_no_key_is_held_with_a_reason(self):
        without = self.ROW.replace("Row: startup_switch ", "")
        reasons = today_page.validate_row(without)
        self.assertEqual(1, len(reasons))
        self.assertIn("declare its own key", reasons[0])

    def test_a_malformed_or_repeated_row_key_is_held(self):
        for ask, needle in (
            ("Row: Startup-Switch ", "lowercase letters"),
            ("Row: one Row: two ", "only one row key"),
        ):
            with self.subTest(ask=ask):
                reasons = today_page.validate_row(
                    self.ROW.replace("Row: startup_switch ", ask)
                )
                self.assertEqual(1, len(reasons))
                self.assertIn(needle, reasons[0])

    def test_a_legacy_row_still_falls_back_to_the_old_derivation(self):
        legacy = (
            "| Old row | test | 2026-08-16 | It is ready. It needs an answer. "
            "Recommendation: do it. Decide: (1) Go? |"
        )
        rows = today_page.parse_review_queue_text(
            "## Active\n" + legacy + "\n", require_keys=False
        )
        self.assertIsNone(rows[0]["row_key"])
        self.assertEqual(
            today_page._legacy_item_id("Old row", "test", "2026-08-16"),
            rows[0]["item_id"],
        )

    def test_closing_a_row_finds_it_by_its_declared_key(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "REVIEW-QUEUE.md"
            queue.write_text(
                "# Queue\n\n## Active\n\n"
                "| Item | Project | Age | Ask |\n|---|---|---|---|\n"
                + self.ROW
                + "\n\n## Closed\n\n"
                "| Item | Closed | Disposition |\n|---|---|---|\n"
            )
            rows = today_page.parse_review_queue(queue)
            today_page.close_review_queue_row(queue, rows[0]["item_id"])
            self.assertEqual([], today_page.parse_review_queue(queue))
            self.assertIn("A card title", queue.read_text())

    def test_each_card_carries_its_row_key(self):
        """The consumer locates the Active row by key, not by title. item_id
        is a hash of the key and cannot be searched for in the queue."""
        rows, manifest = self.manifest_for(self.ROW)
        card = manifest["cards"][0]
        self.assertEqual("startup_switch", card["row_key"])
        self.assertEqual(today_page.row_item_id(card["row_key"]), card["item_id"])
        legacy = today_page.parse_review_queue_text(
            "## Active\n"
            "| Old row | test | 2026-08-16 | It is ready. It needs an answer. "
            "Recommendation: do it. Decide: (1) Go? |\n",
            require_keys=False,
        )
        old = today_page.build_manifest(
            "2026-08-16", [], legacy, {"queue": "legacy"}, require_keys=False
        )
        self.assertIsNone(old["cards"][0]["row_key"])


class PerQuestionRecommendationTests(unittest.TestCase):
    """The reader's pen, 2026-09-14: a card-level recommendation covers the first
    question and says nothing about the second. Every question on a
    multi-question card carries its own recommendation, printed with it."""

    TWO_WITH_BOTH = (
        "| Theme editor | test | 2026-09-14 | The editor is designed and two "
        "calls are open. Recommendation: browser first, appearance only. "
        "Decide: (1:storage) [choice: A = browser first, B = server from day "
        "one] Where does it save? "
        "Recommended: A because ownership rules have to exist before a theme "
        "can be shared. "
        "(2:scope) [choice: A = appearance only, B = also layout] What does a "
        "theme cover? "
        "Recommended: A because layout already lives in the existing "
        "settings. "
        "Row: theme_editor Read: " + READING + " |"
    )
    TWO_WITH_ONE = (
        "| Theme editor | test | 2026-09-14 | The editor is designed and two "
        "calls are open. Recommendation: browser first, appearance only. "
        "Decide: (1:storage) [choice: A = browser first, B = server from day "
        "one] Where does it save? "
        "Recommended: A because ownership rules have to exist before a theme "
        "can be shared. "
        "(2:scope) [choice: A = appearance only, B = also layout] What does a "
        "theme cover? "
        "Row: theme_editor Read: " + READING + " |"
    )
    ONE_WITHOUT = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Row: one_call Read: " + READING + " |"
    )
    ONE_WITH = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: yes because nothing else waits on it. "
        "Row: one_call Read: " + READING + " |"
    )

    def rows(self, row):
        return today_page.parse_review_queue_text("## Active\n" + row + "\n")

    def test_a_second_question_without_its_own_recommendation_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "each question needs its own recommendation",
        ):
            self.rows(self.TWO_WITH_ONE)

    def test_the_held_reason_reaches_the_cover_in_plain_words(self):
        errors = today_page.validate_row(self.TWO_WITH_ONE)
        self.assertEqual(1, len(errors))
        plain = today_page._plain_held_reason(errors[0])
        self.assertIn("each of its questions still needs its own", plain)
        for jargon in ("Recommended:", "Decide", "question key", "Row:"):
            self.assertNotIn(jargon, plain)

    def test_both_recommendations_are_parsed_onto_their_own_questions(self):
        forms = self.rows(self.TWO_WITH_BOTH)[0]["decision_forms"]
        self.assertEqual(
            [
                "A because ownership rules have to exist before a theme can "
                "be shared.",
                "A because layout already lives in the existing settings.",
            ],
            [form["recommendation"] for form in forms],
        )
        self.assertEqual(
            ["Where does it save?", "What does a theme cover?"],
            [form["question"] for form in forms],
        )

    def test_a_single_question_card_needs_no_per_question_clause(self):
        forms = self.rows(self.ONE_WITHOUT)[0]["decision_forms"]
        self.assertEqual(1, len(forms))
        self.assertIsNone(forms[0]["recommendation"])
        block = today_page.build_manifest(
            "2026-09-14", [], self.rows(self.ONE_WITHOUT), {"queue": "one"}
        )
        questions = [
            block_
            for card in block["cards"]
            for fragment in card["fragments"]
            for block_ in fragment["blocks"]
            if block_["kind"] == "question"
        ]
        self.assertEqual([[]], [q["recommendation_lines"] for q in questions])

    def test_a_single_question_card_may_still_carry_one(self):
        forms = self.rows(self.ONE_WITH)[0]["decision_forms"]
        self.assertEqual(
            "yes because nothing else waits on it.", forms[0]["recommendation"]
        )

    def test_each_recommendation_sits_under_its_question_above_its_boxes(self):
        manifest = today_page.build_manifest(
            "2026-09-14", [], self.rows(self.TWO_WITH_BOTH), {"queue": "two"}
        )
        questions = [
            block
            for card in manifest["cards"]
            for fragment in card["fragments"]
            for block in fragment["blocks"]
            if block["kind"] == "question"
        ]
        self.assertEqual(2, len(questions))
        for block in questions:
            self.assertTrue(block["recommendation_lines"])
            self.assertTrue(
                block["recommendation_lines"][0].startswith("Recommended: ")
            )
            text_height = (
                len(block["lines"])
                + len(block["option_lines"])
                + len(block["recommendation_lines"])
            ) * today_page.BODY_LINE_HEIGHT
            # the boxes sit below every line of the block's text, the
            # recommendation included
            self.assertLess(
                max(box["bbox"][3] for box in block["boxes"]),
                block["y_top"] - text_height + today_page.BODY_LINE_HEIGHT,
            )

    def test_the_printed_pdf_shows_each_recommendation_under_its_question(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        manifest = today_page.build_manifest(
            "2026-09-14", [], self.rows(self.TWO_WITH_BOTH), {"queue": "two"}
        )
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "card.pdf"
            today_page.render_pdf(pdf, manifest)
            out = Path(directory) / "card.txt"
            subprocess.run(
                ["pdftotext", "-layout", str(pdf), str(out)], check=True
            )
            text = " ".join(out.read_text().split())
        first = text.index("Where does it save?")
        second = text.index("What does a theme cover?")
        rec_one = text.index("Recommended: A because ownership rules")
        rec_two = text.index("Recommended: A because layout already lives")
        self.assertLess(first, rec_one)
        self.assertLess(rec_one, second)
        self.assertLess(second, rec_two)

    def test_the_answered_gate_still_fires_before_this_one(self):
        answered = self.TWO_WITH_BOTH.replace(
            "Row: theme_editor",
            "Answered: storage on 2026-09-13 by pen Row: theme_editor",
        )
        with self.assertRaisesRegex(
            today_page.TodayPageError, "already carries a recorded answer"
        ):
            self.rows(answered)

    def test_a_stale_github_check_still_holds_the_row(self):
        stale = self.TWO_WITH_BOTH.replace(
            "The editor is designed",
            "The pull request example/widget#12 is open and the editor is "
            "designed",
        ).replace(
            "Row: theme_editor",
            "GH-checked: example/widget#12 OPEN updated 2026-09-10, "
            "checked 2026-09-10 Row: theme_editor",
        )
        with self.assertRaises(today_page.TodayPageError):
            today_page.parse_review_queue_text(
                "## Active\n" + stale + "\n", render_date="2026-09-14"
            )

    # The cross-reviewer's review, 2026-09-14: the per-question recommendation gate
    # required a nonempty "Recommended: X because ..." clause but never
    # checked X against the question's own form. "Recommended: C" passed
    # on an A/B question, and a second sentence after the clause passed
    # despite the one-sentence rule.

    UNAVAILABLE_LETTER = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [choice: A = keep it, B = drop it] Which one? "
        "Recommended: C because it is the best of the three. "
        "Row: one_call Read: " + READING + " |"
    )
    SECOND_SENTENCE = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: yes because nothing else waits on it. Do it today. "
        "Row: one_call Read: " + READING + " |"
    )
    YES_NO_RECOMMENDS_LETTER = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: A because it is the best choice. "
        "Row: one_call Read: " + READING + " |"
    )
    WRITE_IN_TOO_LONG = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [write-in] When? "
        "Recommended: "
        + ("a" * 81)
        + " because the work is stopped until then. "
        "Row: one_call Read: " + READING + " |"
    )
    CHOICE_NO_PREFERENCE = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [choice: A = keep it, B = drop it] Which one? "
        "Recommended: no preference, both are workable because either is "
        "fine. "
        "Row: one_call Read: " + READING + " |"
    )
    YES_NO_NO_PREFERENCE = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: no preference, both are workable because either is "
        "fine. "
        "Row: one_call Read: " + READING + " |"
    )
    WRITE_IN_SHORT = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [write-in] When? "
        "Recommended: this week because the work is stopped until then. "
        "Row: one_call Read: " + READING + " |"
    )

    def test_a_choice_recommendation_naming_an_unavailable_letter_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "question 1 recommends C but the choices are A and B",
        ):
            self.rows(self.UNAVAILABLE_LETTER)

    def test_a_recommendation_with_a_second_sentence_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "more than one sentence",
        ):
            self.rows(self.SECOND_SENTENCE)

    def test_a_yes_no_recommendation_naming_a_letter_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "question 1 recommends A but this question only takes yes or no",
        ):
            self.rows(self.YES_NO_RECOMMENDS_LETTER)

    def test_a_write_in_recommendation_over_80_characters_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "longer than 80 characters",
        ):
            self.rows(self.WRITE_IN_TOO_LONG)

    def test_a_choice_recommendation_may_say_no_preference(self):
        forms = self.rows(self.CHOICE_NO_PREFERENCE)[0]["decision_forms"]
        self.assertEqual(
            "no preference, both are workable because either is fine.",
            forms[0]["recommendation"],
        )

    def test_a_yes_no_recommendation_may_say_no_preference(self):
        forms = self.rows(self.YES_NO_NO_PREFERENCE)[0]["decision_forms"]
        self.assertEqual(
            "no preference, both are workable because either is fine.",
            forms[0]["recommendation"],
        )

    def test_a_write_in_recommendation_under_80_characters_is_accepted(self):
        forms = self.rows(self.WRITE_IN_SHORT)[0]["decision_forms"]
        self.assertEqual(
            "this week because the work is stopped until then.",
            forms[0]["recommendation"],
        )

    def test_a_yes_no_recommendation_of_yes_is_accepted(self):
        forms = self.rows(self.ONE_WITH)[0]["decision_forms"]
        self.assertEqual(
            "yes because nothing else waits on it.", forms[0]["recommendation"]
        )

    def test_a_choice_recommendation_naming_an_offered_letter_is_accepted(self):
        forms = self.rows(self.TWO_WITH_BOTH)[0]["decision_forms"]
        self.assertEqual(
            "A because ownership rules have to exist before a theme can "
            "be shared.",
            forms[0]["recommendation"],
        )

    # The cross-reviewer's re-check, 2026-09-14: (1) a bare "Recommended: A." with no
    # "because" passed and printed with no reason at all; (2) a quoted
    # two-sentence reason passed because the quote mask hid both of its
    # periods from the one-sentence count.

    BARE_ANSWER = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: yes. "
        "Row: one_call Read: " + READING + " |"
    )
    QUOTED_TWO_SENTENCES = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        'Recommended: yes because "First. Second." is the rationale. '
        "Row: one_call Read: " + READING + " |"
    )
    ABBREVIATION_AND_VERSION = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: yes because the tool already ships this behavior "
        "(e.g. through v1.6.6) in production. "
        "Row: one_call Read: " + READING + " |"
    )
    BARE_NO_PREFERENCE = (
        "| One call | test | 2026-09-14 | It is ready and needs one answer. "
        "Recommendation: yes because nothing else waits on it. "
        "Decide: (1:only) [yes/no] Go ahead? "
        "Recommended: no preference, both are workable. "
        "Row: one_call Read: " + READING + " |"
    )

    def test_a_bare_recommendation_with_no_because_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "recommendation with no reason",
        ):
            self.rows(self.BARE_ANSWER)

    def test_a_quoted_two_sentence_reason_is_held(self):
        with self.assertRaisesRegex(
            today_page.TodayPageError,
            "more than one sentence",
        ):
            self.rows(self.QUOTED_TWO_SENTENCES)

    def test_a_reason_with_an_abbreviation_and_a_version_still_passes(self):
        forms = self.rows(self.ABBREVIATION_AND_VERSION)[0]["decision_forms"]
        self.assertEqual(
            "yes because the tool already ships this behavior (e.g. "
            "through v1.6.6) in production.",
            forms[0]["recommendation"],
        )

    def test_the_bare_no_preference_form_is_accepted(self):
        forms = self.rows(self.BARE_NO_PREFERENCE)[0]["decision_forms"]
        self.assertEqual(
            "no preference, both are workable.",
            forms[0]["recommendation"],
        )


class DeferredSameDayTests(unittest.TestCase):
    """mc-j5dr4. The reader 2026-09-07, his words: "if my decision is to defer, then
    it does not get put into the follow up document the same day, but rather
    it actually gets deferred."

    The follow-up document rendered after he pens a Decisions document is the
    place that rule bites: before this, a card he had just deferred at 9 AM
    came straight back on the 9:05 replacement, because "deferred" was not a
    settled state. It is settled for the day it was deferred and open again
    the next morning, unchanged.

    The fixture below carries one card per kind of answer he gives: a plain
    answer, a DEFER, a REMOVE (the DISCARD box), and a penned question
    (needs_clarification). A card whose facts went stale never reaches this
    function at all - the verifier holds its row out of the queue, which is
    covered by the held-row tests above and by
    remarkable/test-decisions-loop.sh.
    """

    DAY = "2026-09-07"
    NEXT_DAY = "2026-09-08"

    ROWS = (
        "| Plain answer card | test | 2026-09-01 | It is ready. It needs an "
        "answer. Recommendation: do it. Decide: (1:go) [yes/no] Ship it? "
        "Row: plain_row Read: " + READING + " |\n"
        "| Deferred card | test | 2026-09-01 | It is ready. It needs an "
        "answer. Recommendation: do it. Decide: (1:go) [yes/no] Ship this one? "
        "Row: defer_row Read: " + READING + " |\n"
        "| Removed card | test | 2026-09-01 | It is ready. It needs an "
        "answer. Recommendation: do it. Decide: (1:go) [yes/no] Ship that one? "
        "Row: remove_row Read: " + READING + " |\n"
        "| Penned question card | test | 2026-09-01 | It is ready. It needs an "
        "answer. Recommendation: do it. Decide: (1:go) [yes/no] Ship the third? "
        "Row: question_row Read: " + READING + " |\n"
    )

    STATES = {
        "Plain answer card": "answered",
        "Deferred card": "deferred",
        "Removed card": "discarded",
        "Penned question card": "needs_clarification",
    }

    def rows(self):
        return today_page.parse_review_queue_text("## Active\n" + self.ROWS)

    def state(self, since=None):
        rows = self.rows()
        entries = {}
        for row in rows:
            entries[today_page._entry_identity(row["item_id"], "go")] = {
                "state": self.STATES[row["title"]],
                "since": since or self.DAY,
                "source": "decisions-" + self.DAY,
                "detail": "penned on the tablet",
            }
        return {"schema_version": 1, "entries": entries}

    def history(self):
        rows = self.rows()
        entries = self.state()["entries"]
        return {
            today_page._entry_identity(row["item_id"], "go"): {
                "item_id": row["item_id"],
                "question_key": "go",
                "question": row["decision_forms"][0]["question"],
            }
            for row in rows
            if entries[today_page._entry_identity(row["item_id"], "go")]["state"]
            in today_page._SETTLED_STATES
        }

    def titles(self, kept):
        return [row["title"] for row in kept]

    def test_a_card_deferred_today_is_off_todays_follow_up_document(self):
        kept, settled, _excluded = today_page.filter_settled_questions(
            self.rows(), self.state(), self.history(), render_date=self.DAY
        )
        self.assertNotIn("Deferred card", self.titles(kept))
        self.assertIn(
            (
                "Deferred card",
                "every question deferred today; it returns tomorrow",
            ),
            [(row["title"], row["reason"]) for row in settled],
        )

    def test_the_same_deferred_card_returns_the_next_morning(self):
        kept, settled, _excluded = today_page.filter_settled_questions(
            self.rows(), self.state(), self.history(), render_date=self.NEXT_DAY
        )
        self.assertIn("Deferred card", self.titles(kept))
        self.assertNotIn(
            "Deferred card", [row["title"] for row in settled]
        )

    def test_answered_and_removed_cards_never_come_back(self):
        for render_date in (self.DAY, self.NEXT_DAY):
            with self.subTest(render_date=render_date):
                kept, _settled, _excluded = today_page.filter_settled_questions(
                    self.rows(), self.state(), self.history(),
                    render_date=render_date,
                )
                self.assertNotIn("Plain answer card", self.titles(kept))
                self.assertNotIn("Removed card", self.titles(kept))

    def test_a_penned_question_stays_in_front_of_him_both_days(self):
        for render_date in (self.DAY, self.NEXT_DAY):
            with self.subTest(render_date=render_date):
                kept, _settled, _excluded = today_page.filter_settled_questions(
                    self.rows(), self.state(), self.history(),
                    render_date=render_date,
                )
                self.assertIn("Penned question card", self.titles(kept))

    def test_without_a_render_date_a_deferred_card_still_prints(self):
        """A caller that passes no render_date gets the behaviour this
        function had before mc-j5dr4: only answered and discarded are settled.
        The generator always passes one, so the day's own document and the
        intraday follow-up both apply the rule.
        """
        kept, _settled, _excluded = today_page.filter_settled_questions(
            self.rows(), self.state(), self.history()
        )
        self.assertIn("Deferred card", self.titles(kept))

    def test_a_card_deferred_yesterday_prints_today(self):
        kept, _settled, _excluded = today_page.filter_settled_questions(
            self.rows(), self.state(since="2026-09-06"), self.history(),
            render_date=self.DAY,
        )
        self.assertIn("Deferred card", self.titles(kept))
