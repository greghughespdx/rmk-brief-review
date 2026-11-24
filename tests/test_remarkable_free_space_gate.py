"""mc-9jd4t: a card cannot print a free-space figure without saying when.

2026-09-13. Card 003 of the Decisions document said "25.2 GiB free, barely
above the 25 GiB floor the monitor watches". By the time the cross-reviewer reviewed it,
df said 24.97 GiB, so the card's claim had reversed while it sat waiting. Free
space moves by the minute.

Two rules, pinned here. A free-space figure must carry the time it was read.
And a card may not say whether that figure is above or below a threshold,
because the comparison is the part that goes stale between rendering and
reading.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "remarkable"))

import today_page  # noqa: E402


class FreeSpaceGate(unittest.TestCase):
    def check(self, text):
        today_page._require_timed_free_space(text)

    def test_untimed_figure_is_refused(self):
        with self.assertRaises(today_page.TodayPageError) as caught:
            self.check("The disk shows 25.2 GiB free on the Mac Studio.")
        self.assertIn("when the reading was taken", str(caught.exception))

    def test_untimed_figure_in_other_units_is_refused(self):
        for figure in ("900 MB free", "1.2 TiB free", "40 GB free"):
            with self.subTest(figure=figure):
                with self.assertRaises(today_page.TodayPageError):
                    self.check(f"There is {figure} on the server.")

    def test_a_clock_time_satisfies_the_rule(self):
        self.check(
            "About 25 GiB free on the Mac Studio when this was written "
            "(16:58 today), against a 25 GiB floor."
        )

    def test_as_of_satisfies_the_rule(self):
        self.check("As of 09:10 today, 25 GiB free on the Mac Studio.")

    def test_comparison_against_a_floor_is_refused(self):
        with self.assertRaises(today_page.TodayPageError) as caught:
            self.check(
                "Measured at 16:58 today: 25.2 GiB free, barely above the "
                "25 GiB floor the monitor watches."
            )
        self.assertIn("above or below a threshold", str(caught.exception))

    def test_below_is_refused_too(self):
        with self.assertRaises(today_page.TodayPageError):
            self.check("As of 16:58, 24 GiB free, just below the 25 GiB floor.")

    def test_a_card_with_no_free_space_figure_passes(self):
        self.check(
            "Eight archives sit here, 11 GB across 16 files, and 25 files "
            "and 17 GB are on the server."
        )

    def test_the_exact_card_that_failed_review_is_refused(self):
        with self.assertRaises(today_page.TodayPageError):
            self.check(
                "The disk shows 25.2 GiB free, barely above the 25 GiB floor "
                "the monitor watches."
            )

    def test_the_rewritten_card_passes(self):
        self.check(
            "About 25 GiB free on the Mac Studio when this was written "
            "(16:58 today), against a 25 GiB floor."
        )


if __name__ == "__main__":
    unittest.main()
