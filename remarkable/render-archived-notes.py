#!/usr/bin/env python3
"""Render the note-region PNGs for archived pages that never got any.

The notes rendering arrived with the mc-1jw9 fix. Pages processed before it
have `strokes.json` and `resolution.json` but no `notes/` directory and no
`notes.json`, so the reader's handwriting from those days exists only as raw stroke
coordinates that no human is going to read. On 2026-08-19 that was true of
2026-07-30, 2026-07-31 and 2026-08-01, seven cards in total.

This backfills them, and only them. It is a PURE ADDITION:

- A processed directory that already has `notes/` or `notes.json` is SKIPPED.
  Nothing archived is ever overwritten. Those files are the record of what
  the reader's device produced on the day.
- A page whose manifest this build cannot read (an older layout version) is
  SKIPPED by name, never forced.
- Dry run by default. `--write` is required to create anything.

Usage:

    render-archived-notes.py --all-archived
    render-archived-notes.py --all-archived --write
    render-archived-notes.py --processed-dir <path> --write
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import today_page as tp  # noqa: E402  (path shim above is deliberate)

REPO_ROOT = tp.REPO_ROOT
TODAY_PAGES = tp.ANSWER_RECORD_ROOT
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

EXIT_OK = 0
EXIT_NOTHING_DONE = 1
EXIT_ERROR = 2


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def find_manifest(processed_dir: Path, page_id: str) -> Path | None:
    for candidate in sorted(processed_dir.parent.glob("*.manifest.json")):
        try:
            if read_json(candidate).get("page_id") == page_id:
                return candidate
        except (OSError, json.JSONDecodeError):
            continue
    return None


def discover(root: Path = TODAY_PAGES) -> list[Path]:
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not DATE_RE.fullmatch(child.name):
            continue
        processed = child / "processed"
        if (processed / "resolution.json").is_file():
            found.append(processed)
    return found


def stroke_centre(stroke: dict[str, Any]) -> tuple[float, float]:
    bbox = tp._stroke_bbox(stroke)
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def card_rect(card: dict[str, Any]) -> list[float]:
    layout = card["layout"]
    return [
        layout["x"],
        layout["bottom"],
        layout["x"] + layout["width"],
        layout["top"],
    ]


def expand_note_regions(
    manifest: dict[str, Any],
    strokes_doc: dict[str, Any],
    resolution: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every stroke that is not an answer tick belongs to a card's note.

    The resolver only attributes ink that falls inside the two ruled note
    lines (extended down to whatever comes next). The reader writes past that: above
    the lines, across the printed card, and in the gaps between cards. Those
    strokes end up in `ignored_strokes`, absent from `note_regions`, and so
    missing from the rendered PNG. Real losses on the archive as of
    2026-08-19: 2026-08-08 card-001 dropped a four-line paragraph (37 strokes)
    and 2026-08-16 card-018 dropped its first line (22 strokes), 191 strokes
    across the whole archive.

    So the capture is inverted. Answer ticks are spoken for; everything else is
    handwriting, and it goes to the card it sits on, or failing that to the
    nearest card on its page. Only a page with no cards at all can leave a
    stroke unassigned.
    """
    tick_ids = {
        str(stroke_id)
        for entry in list(resolution.get("decisions", []))
        + list(resolution.get("actions", []))
        for stroke_id in entry.get("stroke_ids", [])
    }
    cards_by_page: dict[int, list[dict[str, Any]]] = {}
    for card in manifest.get("cards", []):
        cards_by_page.setdefault(card["page"], []).append(card)
    cards_by_id = {card["card_id"]: card for card in manifest.get("cards", [])}

    regions: dict[str, dict[str, Any]] = {}
    for region in resolution.get("note_regions", []):
        regions[region["card_id"]] = {
            "card_id": region["card_id"],
            "page": region["page"],
            "label": region["label"],
            "stroke_ids": list(region["stroke_ids"]),
        }
    seen = {sid for region in regions.values() for sid in region["stroke_ids"]}

    unassigned: list[str] = []
    for stroke in strokes_doc.get("strokes", []):
        stroke_id = str(stroke.get("stroke_id"))
        if stroke_id in tick_ids or stroke_id in seen:
            continue
        page = stroke.get("page")
        page_cards = cards_by_page.get(page, [])
        if not page_cards:
            unassigned.append(stroke_id)
            continue
        cx, cy = stroke_centre(stroke)
        card = next(
            (c for c in page_cards if tp._inside(cx, cy, card_rect(c))),
            None,
        )
        if card is None:
            # Written in a margin or the gap between cards: it belongs to
            # whichever card it is closest to, the way a reader would judge it.
            def distance(candidate: dict[str, Any]) -> float:
                x0, y0, x1, y1 = card_rect(candidate)
                dx = max(x0 - cx, 0.0, cx - x1)
                dy = max(y0 - cy, 0.0, cy - y1)
                return (dx * dx + dy * dy) ** 0.5

            card = min(page_cards, key=distance)
        region = regions.setdefault(
            card["card_id"],
            {
                "card_id": card["card_id"],
                "page": card["page"],
                "label": cards_by_id[card["card_id"]].get("headline", ""),
                "stroke_ids": [],
            },
        )
        region["stroke_ids"].append(stroke_id)
        seen.add(stroke_id)

    ordered = sorted(regions.values(), key=lambda r: (r["page"], r["card_id"]))
    for region in ordered:
        region["stroke_ids"] = sorted(set(region["stroke_ids"]))
    return ordered, unassigned


def account_for_every_stroke(
    manifest: dict[str, Any],
    strokes_doc: dict[str, Any],
    resolution: dict[str, Any],
) -> list[str]:
    """Stroke ids that neither a note nor an answer tick claims. Should be empty."""
    regions, unassigned = expand_note_regions(manifest, strokes_doc, resolution)
    claimed = {sid for region in regions for sid in region["stroke_ids"]}
    claimed |= {
        str(stroke_id)
        for entry in list(resolution.get("decisions", []))
        + list(resolution.get("actions", []))
        for stroke_id in entry.get("stroke_ids", [])
    }
    everything = {
        str(stroke.get("stroke_id")) for stroke in strokes_doc.get("strokes", [])
    }
    return sorted((everything - claimed) | set(unassigned))


def region_file_name(region: dict[str, Any]) -> str:
    """The PNG name today_page.render_note_region_pngs will give this region.

    mc-z5hy3: a schema-1 region is named for its card; a schema-2 region
    carries its own path because it is a cluster of ink, not a card's line.
    """
    if region.get("card_id") is not None and not region.get("path"):
        return f"{region['card_id']}.png"
    return Path(str(region.get("path") or f"{region.get('card_id')}.png")).name


class Outcome:
    def __init__(self, processed_dir: Path) -> None:
        self.processed_dir = processed_dir
        self.schema_version = 1
        self.date = processed_dir.parent.name
        self.status = "pending"
        self.reason = ""
        self.regions: list[dict[str, Any]] = []
        self.original_regions: list[dict[str, Any]] = []
        self.recovered: list[str] = []
        self.unassigned: list[str] = []
        self.written: list[Path] = []
        self.recut = False
        self.out_dir: Path | None = None
        self.index_path: Path | None = None


def plan(processed_dir: Path) -> Outcome:
    """Decide what this directory needs, touching nothing."""
    outcome = Outcome(processed_dir)
    resolution_path = processed_dir / "resolution.json"
    strokes_path = processed_dir / "strokes.json"

    if not strokes_path.is_file():
        outcome.status = "skipped"
        outcome.reason = "no strokes.json, so there is no ink to render"
        return outcome

    try:
        stored = read_json(resolution_path)
    except (OSError, json.JSONDecodeError) as exc:
        outcome.status = "error"
        outcome.reason = f"resolution.json is unreadable ({exc})"
        return outcome

    page_id = str(stored.get("page_id", ""))
    manifest_path = find_manifest(processed_dir, page_id)
    if manifest_path is None:
        outcome.status = "skipped"
        outcome.reason = f"no manifest on disk matches page {page_id}"
        return outcome

    try:
        manifest = read_json(manifest_path)
        strokes = read_json(strokes_path)
        resolution = tp.resolve(manifest, strokes)
    except tp.TodayPageError as exc:
        outcome.status = "skipped"
        outcome.reason = f"this build cannot read that page ({exc})"
        return outcome
    except Exception as exc:  # noqa: BLE001 - report, never force
        outcome.status = "error"
        outcome.reason = f"replay failed ({type(exc).__name__}: {exc})"
        return outcome

    if resolution.get("schema_version") == 2:
        # mc-z5hy3: a schema-2 page gets its note regions from the v3 resolver
        # itself - every stroke no answer claimed, clustered by proximity with
        # its own bounds - so there is nothing to re-attribute to cards here.
        # Replaying the page IS the backfill for a v3 day processed before
        # that fix, so those regions are taken as they come.
        regions, unassigned = list(resolution.get("note_regions") or []), []
    else:
        regions, unassigned = expand_note_regions(manifest, strokes, resolution)
    outcome.regions = regions
    outcome.unassigned = unassigned
    outcome.original_regions = resolution.get("note_regions", [])
    original_ids = {
        sid for region in outcome.original_regions for sid in region["stroke_ids"]
    }
    outcome.schema_version = int(resolution.get("schema_version") or 1)
    outcome.recovered = sorted(
        {sid for region in regions for sid in region["stroke_ids"]} - original_ids
    )
    # Never overwrite: an existing rendering keeps its name and the corrected
    # one lands beside it under a dated name.
    existing = (processed_dir / "notes").exists() or (
        processed_dir / "notes.json"
    ).exists()
    outcome.recut = existing
    if existing:
        stamp = dt.date.today().isoformat()
        target = processed_dir / f"notes-recut-{stamp}"
        counter = 2
        while target.exists() or target.with_suffix(".json").exists():
            target = processed_dir / f"notes-recut-{stamp}-{counter}"
            counter += 1
        outcome.out_dir = target
        outcome.index_path = target.with_suffix(".json")
    else:
        outcome.out_dir = processed_dir / "notes"
        outcome.index_path = processed_dir / "notes.json"
    outcome.status = "would-render" if outcome.regions else "nothing-to-render"
    outcome._manifest = manifest  # type: ignore[attr-defined]
    outcome._strokes = strokes  # type: ignore[attr-defined]
    outcome._resolution = resolution  # type: ignore[attr-defined]
    return outcome


def claim_directory(path: Path) -> bool:
    """Take exclusive ownership of a destination name, or report it taken.

    mkdir either creates the directory or fails: there is no window between
    the two, unlike checking that a path is free and then writing to it. Two
    runs that reach this line at the same moment cannot both win.
    """
    try:
        path.mkdir(parents=True)
        return True
    except FileExistsError:
        return False


def render(outcome: Outcome) -> Outcome:
    """Write the images. NEVER over anything that is already on disk.

    Two things make that true under concurrency (mc-63x2 round-2 finding 6):

    - Staging is unique per run. A fixed .<name>.staging path is a channel
      between runs: a second run renders into the first one's directory, and a
      run that died mid-render leaves files there that the next run publishes
      as its own.
    - Publication claims the destination with mkdir, which is atomic, instead
      of checking that it is free and writing to it afterwards. The loser of
      that race publishes nothing.
    """
    out_dir, index_path = outcome.out_dir, outcome.index_path
    assert out_dir is not None and index_path is not None
    if out_dir.exists() or index_path.exists():
        outcome.status = "skipped"
        outcome.reason = (
            f"{out_dir} appeared while this run was working; nothing archived "
            "is ever overwritten"
        )
        return outcome
    # render_note_region_pngs writes into <staging>/notes, so give it a parent
    # nobody else can be using: this run, this process, this attempt.
    staging = out_dir.parent / (
        f".{out_dir.name}.staging-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    try:
        rendered = tp.render_note_region_pngs(
            outcome._manifest,  # type: ignore[attr-defined]
            outcome._strokes,  # type: ignore[attr-defined]
            {"note_regions": outcome.regions},
            staging,
        )
        # The barrier. Everything above this line is in a directory only this
        # run can see; everything below is published.
        if not claim_directory(out_dir):
            outcome.status = "skipped"
            outcome.reason = (
                f"{out_dir} was claimed by another run while this one was "
                "rendering; nothing archived is ever overwritten"
            )
            return outcome
        published: list[dict[str, Any]] = []
        for item in rendered:
            source = Path(item["path"])
            destination = out_dir / source.name
            os.rename(source, destination)
            published.append({**item, "path": str(destination)})
        # The index is created exclusively too, so a name can never be
        # published twice even if the directory claim were somehow bypassed.
        try:
            handle = os.open(
                index_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
        except FileExistsError:
            outcome.status = "skipped"
            outcome.reason = (
                f"{index_path} appeared while this run was publishing; "
                "nothing archived is ever overwritten"
            )
            return outcome
        os.close(handle)
        tp.atomic_json(index_path, published)
        outcome.written = [Path(item["path"]) for item in published]
        outcome.written.append(index_path)
        outcome.status = "rendered"
        return outcome
    finally:
        # This run's scratch space, and nothing else, whatever happened above.
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)


def report(outcomes: list[Outcome], wrote: bool) -> str:
    lines = [f"render-archived-notes ({'WRITE' if wrote else 'DRY RUN'})"]
    cards = 0
    for outcome in outcomes:
        lines.append("")
        lines.append(f"{outcome.date}  {outcome.processed_dir}")
        if outcome.status in ("skipped", "error", "nothing-to-render"):
            lines.append(f"  {outcome.status.upper()}: {outcome.reason or 'no notes'}")
            continue
        lines.append(
            f"  {len(outcome.regions)} note cards "
            f"{'rendered' if wrote else 'would be rendered'}"
            + (
                f" as a RECUT beside the existing {outcome.processed_dir / 'notes'}"
                if outcome.recut
                else ""
            )
            + ":"
        )
        if outcome.recovered:
            lines.append(
                f"    recovers {len(outcome.recovered)} strokes the original "
                "rendering left out"
            )
        if outcome.unassigned:
            lines.append(
                f"    WARNING: {len(outcome.unassigned)} strokes sit on a page "
                "with no cards and are in no image"
            )
        for region in outcome.regions:
            cards += 1
            name = region_file_name(region)
            original = next(
                (
                    r
                    for r in outcome.original_regions
                    if region_file_name(r) == name
                ),
                None,
            )
            gained = len(region["stroke_ids"]) - (
                len(original["stroke_ids"]) if original else 0
            )
            lines.append(
                f"    {region.get('label', '')} ({name}, "
                f"{len(region['stroke_ids'])} strokes"
                + (f", +{gained} recovered" if gained else "")
                + f") -> {outcome.out_dir / name}"
            )
    lines.append("")
    lines.append(
        f"TOTAL {cards} note cards "
        f"{'rendered' if wrote else 'would be rendered'} across "
        f"{sum(1 for o in outcomes if o.regions and o.status in ('rendered', 'would-render'))} documents"
    )
    if not wrote and cards:
        lines.append("Nothing was written. Re-run with --write to create them.")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--processed-dir", action="append", default=[])
    parser.add_argument("--all-archived", action="store_true")
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually create notes/ and notes.json (default is a dry run)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    dirs = [Path(value) for value in args.processed_dir]
    if args.all_archived:
        dirs.extend(discover())
    if not dirs:
        print(
            "render-archived-notes: give --processed-dir PATH or --all-archived",
            file=sys.stderr,
        )
        return EXIT_ERROR

    outcomes = [plan(Path(path)) for path in dirs]
    if args.write:
        for outcome in outcomes:
            if outcome.status == "would-render":
                render(outcome)

    print(report(outcomes, args.write))
    if any(outcome.status == "error" for outcome in outcomes):
        return EXIT_ERROR
    if args.write and not any(outcome.status == "rendered" for outcome in outcomes):
        return EXIT_NOTHING_DONE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
