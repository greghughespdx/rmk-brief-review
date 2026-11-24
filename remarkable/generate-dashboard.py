#!/usr/bin/env python3
"""Morning document generator (mc-06gp; reworked 2026-07-28 per the reader's consensus).

Two modes:
  --mode briefing (the survivor): Morning Briefing = the overnight handoff's
    "## Morning Briefing" prose, then a Today's status block (calendar, service
    health, weather). The decisions list does NOT render here - it lives in the
    separate Decisions document (full cards, tick boxes).
  --mode dashboard (legacy, retired from the daily flow): the old one-page
    dashboard, kept only for manual regeneration of historical shape.

Every section degrades gracefully: a failed source renders as a single honest
"unavailable" line, never a crash and never fabricated data.

Usage:
  generate-dashboard.py [--calendar-file FILE] [-o OUT.md]

Output defaults to /tmp/morning-dashboard-YYYY-MM-DD.md. Push with:
  push-brief.sh <out.md> --dest /Briefs
"""
import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rmw_config  # noqa: E402

_CONFIG = rmw_config.load()
# The folder that holds the overnight briefing files (briefing.source_dir).
MC = Path(_CONFIG.get("briefing.source_dir"))
HANDOFF_GLOB = str(_CONFIG.get("briefing.handoff_pattern")).replace("{date}", "*")


def _probes():
    """(name shown to the reader, url, any_http_counts) from briefing.health_probes.

    Each entry is "Name|URL" or "Name|URL|any". any_http_counts=True treats ANY
    HTTP status as alive (host-strict apps answer 400 to direct-IP probes by
    design); otherwise 200-399 = up. Timeouts and refusals are DOWN either way.
    """
    out = []
    for entry in _CONFIG.get("briefing.health_probes") or []:
        parts = [part.strip() for part in str(entry).split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            out.append((parts[0], parts[1], len(parts) > 2 and parts[2].lower() == "any"))
    return out


HTTP_PROBES = _probes()

# mc-saxfl: the disk-inventory job (tools/disk-inventory/) writes its
# plain-English report here every run. This section just reads it -- same
# input-contract pattern as section_calendar reading a file, degrading
# gracefully if the report is missing or stale rather than crashing or
# fabricating a number.
DISK_REPORT = Path.home() / "Library" / "Application Support" / "remarkable-workflow" / "disk-inventory" / "latest-report.md"
DISK_REPORT_MAX_AGE_HOURS = 36

WEATHER_LAT = str(_CONFIG.get("briefing.weather_lat") or "")
WEATHER_LON = str(_CONFIG.get("briefing.weather_lon") or "")
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
       45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
       55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
       81: "rain showers", 82: "heavy showers", 95: "thunderstorms"}


def fetch(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "mc-dashboard/1"})
    return urllib.request.urlopen(req, timeout=timeout)


def section_calendar(calendar_file):
    if not calendar_file:
        return "_Calendar unavailable this morning (source not connected)._"
    try:
        text = Path(calendar_file).read_text().strip()
        return text if text else "_No events on today's calendar._"
    except OSError:
        return "_Calendar unavailable this morning (file unreadable)._"


def section_decisions():
    """Render the COMPLETE active review queue, grouped by section.

    EVERY item carries its one-line ask, including Triage - the reader marked the
    bare-filename Triage list "non-actionable as written" (2026-07-27 ink,
    mc-iuw9); his '?' pen mark means "not enough info to act". A count without
    the list is not actionable either (the reader, 2026-07-26), so nothing is
    truncated - the PDF may run past one page and that is fine.
    """
    try:
        lines = (MC / "REVIEW-QUEUE.md").read_text().splitlines()
    except OSError:
        return "_Review queue unreadable._"
    sections, current = {}, None  # section title -> [(item, ask)]
    order = []
    for ln in lines:
        if ln.startswith("## "):
            if ln.startswith("## Active"):
                current = re.sub(r"^## Active\s*[-—]\s*", "", ln).split("(")[0].strip()
                if current not in sections:
                    sections[current] = []
                    order.append(current)
            else:
                current = None
            continue
        if current and ln.startswith("|") and "---" not in ln and "| Item |" not in ln:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 4:
                item = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[0])
                ask = re.sub(r"\*\*", "", cells[3])
                sections[current].append((item[:90], ask.split(". ")[0][:200]))
    total = sum(len(v) for v in sections.values())
    if total == 0:
        return "_Nothing is waiting on you._"
    out = [f"**{total} items are waiting on you.**"]
    for title in order:
        rows = sections[title]
        if not rows:
            continue
        out += ["", f"**{title} ({len(rows)})**", ""]
        for item, ask in rows:
            out.append(f"- **{item}** - {ask}" if ask else f"- {item}")
    return "\n".join(out)


def section_health():
    if not HTTP_PROBES:
        return "_No service checks are configured._"
    out = []
    for name, url, any_http in HTTP_PROBES:
        ok = False
        for timeout in (8, 12):  # one retry for slow morning starts
            try:
                code = fetch(url, timeout=timeout).status
                ok = True if any_http else 200 <= code < 400
                break
            except urllib.error.HTTPError as e:
                ok = any_http or 200 <= e.code < 400
                break
            except Exception:
                continue
        out.append(f"- {'OK' if ok else '** DOWN **'} - {name}")
    return "\n".join(out)


def section_overnight():
    handoffs = sorted(MC.glob(HANDOFF_GLOB))
    if not handoffs:
        return "_No overnight handoff found._"
    latest = handoffs[-1]
    date = latest.name[:10]
    text = latest.read_text()
    m = re.search(r"## Morning Briefing\n(.*?)(?=\n## )", text, re.S)
    if date == datetime.date.today().isoformat() and m:
        return f"_Full briefing is in this morning's separate Morning Briefing doc._"
    return f"_Latest overnight handoff is from {date}; today's session summary arrives with the morning briefing._"


def section_disk():
    """The latest tools/disk-inventory/ report, embedded as-is (mc-saxfl).

    Degrades gracefully like every other section here: a missing, unreadable,
    empty, or stale report renders one honest line, never a crash and never a
    fabricated size.
    """
    try:
        mtime = datetime.datetime.fromtimestamp(DISK_REPORT.stat().st_mtime)
    except OSError:
        return "_Disk inventory unavailable (no report yet; tools/disk-inventory/install.sh has not run)._"
    try:
        text = DISK_REPORT.read_text().strip()
    except OSError:
        return "_Disk inventory unavailable (report unreadable)._"
    if not text:
        return "_Disk inventory unavailable (report is empty)._"
    age_hours = (datetime.datetime.now() - mtime).total_seconds() / 3600
    # Drop the report's own top-level "# Mac Studio Disk Inventory - ..." line;
    # "### Disk" (below) is that heading here. Shift the report's "##"
    # subsections to "####" so they nest correctly under "### Disk" instead
    # of outranking it.
    body = re.sub(r"^#\s+.*\n+", "", text, count=1).strip()
    body = re.sub(r"^##(?!#)", "####", body, flags=re.M)
    if age_hours > DISK_REPORT_MAX_AGE_HOURS:
        return f"_Disk inventory is stale (last run {age_hours:.0f}h ago; the scheduled job may not be running)._\n\n{body}"
    return body


def section_weather():
    if not (WEATHER_LAT and WEATHER_LON):
        return "_No forecast location is configured._"
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}"
           f"&longitude={WEATHER_LON}&daily=temperature_2m_max,temperature_2m_min,"
           f"precipitation_probability_max,weather_code&temperature_unit=fahrenheit"
           f"&timezone=auto&forecast_days=2")
    try:
        d = json.load(fetch(url))["daily"]
        def day(i, label):
            desc = WMO.get(d["weather_code"][i], "mixed")
            return (f"- **{label}:** {desc}, high {round(d['temperature_2m_max'][i])}F, "
                    f"low {round(d['temperature_2m_min'][i])}F, "
                    f"{d['precipitation_probability_max'][i]}% chance of rain")
        return "\n".join([day(0, "Today"), day(1, "Tomorrow")])
    except Exception:
        return "_Forecast unavailable this morning._"


def briefing_body(briefing_file):
    """The overnight handoff's Morning Briefing prose, or an honest absence line."""
    if briefing_file:
        try:
            text = Path(briefing_file).read_text().strip()
            if text:
                return text
        except OSError:
            pass
    handoffs = sorted(MC.glob(HANDOFF_GLOB))
    if handoffs:
        latest = handoffs[-1]
        m = re.search(r"## Morning Briefing\n(.*?)(?=\n## )", latest.read_text(), re.S)
        if m and latest.name[:10] == datetime.date.today().isoformat():
            return m.group(1).strip()
    return "_No overnight briefing was produced for this morning._"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["briefing", "dashboard"], default="briefing")
    ap.add_argument("--calendar-file")
    ap.add_argument("--briefing-file", help="pre-extracted Morning Briefing body (briefing mode)")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    today = datetime.date.today()

    if args.mode == "briefing":
        out_path = Path(args.out or f"/tmp/morning-briefing-{today.isoformat()}.md")
        doc = f"""# Morning Briefing - {today.strftime('%A, %B %d, %Y')}

{briefing_body(args.briefing_file)}

## Today's status

### Calendar

{section_calendar(args.calendar_file)}

### Service health

{section_health()}

### Disk

{section_disk()}

### Weather

{section_weather()}

_Your decisions are in the separate Decisions document, one card each, with answer boxes._
"""
    else:
        out_path = Path(args.out or f"/tmp/morning-dashboard-{today.isoformat()}.md")
        doc = f"""# Morning Dashboard - {today.strftime('%A, %B %d, %Y')}

## Today's calendar

{section_calendar(args.calendar_file)}

## Waiting on you

{section_decisions()}

## Service health

{section_health()}

## Overnight

{section_overnight()}

## Weather

{section_weather()}
"""
    out_path.write_text(doc)
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
