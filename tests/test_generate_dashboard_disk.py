"""Tests for generate-dashboard.py's Disk section (mc-saxfl, the cross-reviewer cross-review B4).

The module file is named with a hyphen (generate-dashboard.py) so it cannot be
imported with a normal `import` statement; it is loaded via importlib, the
same technique tools/drift-monitor/test_*.py already use for their runner.

Covers section_disk() in isolation (missing, unreadable, empty, stale, fresh
reports; heading removal and nesting) and one mode-level assertion that the
rendered briefing output actually contains the Disk section.
"""

import importlib.util
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "remarkable"
SPEC = importlib.util.spec_from_file_location("generate_dashboard", HERE / "generate-dashboard.py")
gd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gd)


SAMPLE_REPORT = """# Mac Studio Disk Inventory - Wednesday, September 09, 2026 20:49 PDT

Read-only inventory. This job never deletes anything; it measures, compares to the last run, and names what a person or another job could clean up.

Free space on /: 26.61 GiB of 460.43 GiB (first run, no prior baseline)

## What grew since the last run

_First run -- no prior baseline to compare against. Sizes below are the starting point._

## Reclaimable caches (nothing deleted by this job)

| Cache | Size | Reclaimable | Action (not run) |
|---|---|---|---|
| Homebrew cache | 42.0 MiB | 42.0 MiB | brew cleanup (not run by this job) |
"""


# ---------------------------------------------------------------------------
# section_disk(): missing, unreadable, empty, stale, fresh
# ---------------------------------------------------------------------------

def test_section_disk_missing_report(monkeypatch, tmp_path):
    monkeypatch.setattr(gd, "DISK_REPORT", tmp_path / "no-such-report.md")
    result = gd.section_disk()
    assert "unavailable" in result
    assert "no report yet" in result


def test_section_disk_unreadable_report(monkeypatch, tmp_path):
    # A directory at the report path: stat() succeeds (so the mtime/age check
    # passes) but read_text() raises OSError (IsADirectoryError) -- a portable
    # way to force the "unreadable" branch without permission hacks.
    report_dir = tmp_path / "latest-report.md"
    report_dir.mkdir()
    monkeypatch.setattr(gd, "DISK_REPORT", report_dir)
    result = gd.section_disk()
    assert "unavailable" in result
    assert "unreadable" in result


def test_section_disk_empty_report(monkeypatch, tmp_path):
    report = tmp_path / "latest-report.md"
    report.write_text("")
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    result = gd.section_disk()
    assert "unavailable" in result
    assert "empty" in result


def test_section_disk_stale_report(monkeypatch, tmp_path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT)
    old = time.time() - (gd.DISK_REPORT_MAX_AGE_HOURS + 5) * 3600
    os.utime(report, (old, old))
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    result = gd.section_disk()
    assert "stale" in result
    # A stale report still shows its content -- staleness is a warning, not a
    # withholding (unlike missing/unreadable/empty, which have nothing to show).
    assert "Homebrew cache" in result


def test_section_disk_fresh_report_has_no_staleness_warning(monkeypatch, tmp_path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT)
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    result = gd.section_disk()
    assert "stale" not in result
    assert "unavailable" not in result
    assert "Homebrew cache" in result
    assert "Free space on /: 26.61 GiB" in result


# ---------------------------------------------------------------------------
# Heading removal and nesting
# ---------------------------------------------------------------------------

def test_section_disk_strips_the_top_level_title(monkeypatch, tmp_path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT)
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    result = gd.section_disk()
    assert "# Mac Studio Disk Inventory" not in result
    assert not result.startswith("#")


def test_section_disk_demotes_h2_sections_to_h4(monkeypatch, tmp_path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT)
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    result = gd.section_disk()
    assert "#### What grew since the last run" in result
    assert "#### Reclaimable caches (nothing deleted by this job)" in result
    # No line is left at the un-demoted "## " (exactly two hashes) level --
    # "#### ..." contains "## ..." as a plain substring, so this must check
    # line starts, not substring membership.
    assert not re.search(r"(?m)^## [^#]", result)


def test_section_disk_never_touches_h3_or_deeper(monkeypatch, tmp_path):
    text = "# Title\n\n## Section Two\n\n### Already H3\n\nbody\n"
    report = tmp_path / "latest-report.md"
    report.write_text(text)
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    result = gd.section_disk()
    assert "#### Section Two" in result
    assert "### Already H3" in result   # unchanged, not further demoted


# ---------------------------------------------------------------------------
# Mode-level: the rendered briefing actually contains the Disk section
# ---------------------------------------------------------------------------

def test_briefing_mode_output_contains_disk_section(monkeypatch, tmp_path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT)
    monkeypatch.setattr(gd, "DISK_REPORT", report)
    monkeypatch.setattr(gd, "HTTP_PROBES", [])              # no live network probes
    monkeypatch.setattr(gd, "section_weather", lambda: "_stub weather_")  # no live network fetch

    briefing_file = tmp_path / "briefing-body.md"
    briefing_file.write_text("Test briefing body.")
    out_file = tmp_path / "out.md"

    monkeypatch.setattr(sys, "argv", [
        "generate-dashboard.py", "--mode", "briefing",
        "--briefing-file", str(briefing_file), "-o", str(out_file),
    ])
    rc = gd.main()
    assert rc == 0
    doc = out_file.read_text()
    assert "### Disk" in doc
    assert "#### What grew since the last run" in doc
    assert "Homebrew cache" in doc
    # heading order sanity: Disk comes before Weather, after Service health
    assert doc.index("### Service health") < doc.index("### Disk") < doc.index("### Weather")


def test_briefing_mode_output_degrades_gracefully_with_no_report(monkeypatch, tmp_path):
    monkeypatch.setattr(gd, "DISK_REPORT", tmp_path / "no-such-report.md")
    monkeypatch.setattr(gd, "HTTP_PROBES", [])
    monkeypatch.setattr(gd, "section_weather", lambda: "_stub weather_")

    briefing_file = tmp_path / "briefing-body.md"
    briefing_file.write_text("Test briefing body.")
    out_file = tmp_path / "out.md"

    monkeypatch.setattr(sys, "argv", [
        "generate-dashboard.py", "--mode", "briefing",
        "--briefing-file", str(briefing_file), "-o", str(out_file),
    ])
    rc = gd.main()
    assert rc == 0
    doc = out_file.read_text()
    assert "### Disk" in doc
    assert "no report yet" in doc
