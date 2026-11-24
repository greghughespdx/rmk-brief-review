#!/usr/bin/env python3
"""Ink resolution for Decisions document manifest schema 2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import today_page as tp

SCHEMA_VERSION = 2
LAYOUT_VERSION = "decisions-v3.0"
FINAL_BOXES = frozenset({"DEFER", "DISCARD"})
# mc-z5hy3: how far apart two pieces of note ink can sit and still be read as
# one piece of handwriting. One answer-box width (24pt) joins the words of a
# line and the lines of a paragraph without joining a note at the top of a
# page to a different note at the bottom.
NOTE_CLUSTER_GAP = 24.0


def _bbox(stroke: dict[str, Any]) -> list[float]:
    return tp._stroke_bbox(stroke)


def _centre(bounds: list[float]) -> tuple[float, float]:
    return ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise tp.TodayPageError("Decisions v3 requires manifest schema version 2.")
    if manifest.get("layout_version") != LAYOUT_VERSION:
        raise tp.TodayPageError("Decisions v3 layout version is not supported.")
    if not isinstance(manifest.get("page_id"), str) or not manifest["page_id"]:
        raise tp.TodayPageError("Manifest page id is missing.")
    page_count = manifest.get("page_count")
    answerable = manifest.get("answerable_page_count")
    answerable_pages = manifest.get("answerable_pages")
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 1
        or isinstance(answerable, bool)
        or not isinstance(answerable, int)
        or not 1 <= answerable <= page_count
    ):
        raise tp.TodayPageError("Manifest page counts are invalid.")
    if (
        not isinstance(answerable_pages, list)
        or len(answerable_pages) != answerable
        or any(
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= page_count
            for page in answerable_pages
        )
        or len(set(answerable_pages)) != len(answerable_pages)
    ):
        raise tp.TodayPageError("Manifest answerable pages are invalid.")
    entries = manifest.get("entries")
    cards = manifest.get("cards")
    if not isinstance(entries, list) or not isinstance(cards, list):
        raise tp.TodayPageError("Manifest entries or cards are missing.")
    entry_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise tp.TodayPageError("Manifest entry is not an object.")
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in entry_ids:
            raise tp.TodayPageError("Manifest entry ids are missing or repeated.")
        entry_ids.add(entry_id)
        if entry.get("kind") == "action":
            if not isinstance(entry.get("label"), str) or not entry["label"]:
                raise tp.TodayPageError(f"Manifest action {entry_id} has no label.")
            if entry.get("page") not in answerable_pages:
                raise tp.TodayPageError(
                    f"Manifest action {entry_id} is not on an answerable page."
                )
            boxes = entry.get("boxes")
            if (
                not isinstance(boxes, list)
                or len(boxes) != 1
                or not isinstance(boxes[0], dict)
                or boxes[0].get("choice") != "RUN"
                or not isinstance(boxes[0].get("bbox"), list)
                or len(boxes[0]["bbox"]) != 4
            ):
                raise tp.TodayPageError(
                    f"Manifest action {entry_id} has invalid RUN geometry."
                )
            action = entry.get("action")
            if not isinstance(action, dict):
                raise tp.TodayPageError(
                    f"Manifest action {entry_id} has no action payload."
                )
            tp.validate_action({**action, "label": entry["label"]})
            continue
        if entry.get("kind") != "decision":
            raise tp.TodayPageError(f"Manifest entry {entry_id} has an invalid kind.")
        for field in ("item_id", "card_id", "question_key", "label", "answer_type"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise tp.TodayPageError(f"Manifest entry {entry_id} has no {field}.")
        if entry["answer_type"] not in {"yes_no", "choice", "write_in"}:
            raise tp.TodayPageError(f"Manifest entry {entry_id} has an invalid answer type.")
        if entry.get("page") not in answerable_pages:
            raise tp.TodayPageError(f"Manifest entry {entry_id} is not on an answerable page.")
        boxes = entry.get("boxes")
        if not isinstance(boxes, list) or len(boxes) < 2:
            raise tp.TodayPageError(f"Manifest entry {entry_id} has invalid boxes.")
        choices = [box.get("choice") for box in boxes if isinstance(box, dict)]
        if len(choices) != len(boxes) or len(set(choices)) != len(choices):
            raise tp.TodayPageError(f"Manifest entry {entry_id} has invalid box choices.")
        if not FINAL_BOXES.issubset(choices):
            raise tp.TodayPageError(f"Manifest entry {entry_id} lacks Defer or Discard.")
        for box in boxes:
            bounds = box.get("bbox")
            if not isinstance(bounds, list) or len(bounds) != 4:
                raise tp.TodayPageError(f"Manifest entry {entry_id} has invalid box geometry.")
        write_in = entry.get("write_in")
        if entry["answer_type"] in {"choice", "write_in"}:
            if not isinstance(write_in, dict) or not isinstance(write_in.get("bbox"), list) or len(write_in["bbox"]) != 4:
                raise tp.TodayPageError(f"Manifest entry {entry_id} needs write-in geometry.")
        elif write_in is not None:
            raise tp.TodayPageError(f"Manifest entry {entry_id} cannot have write-in geometry.")
    if manifest.get("manifest_hash") != tp.manifest_digest(manifest):
        raise tp.TodayPageError("Manifest integrity check failed. No ink was read.")


def _stroke_clusters(
    strokes: list[dict[str, Any]], box_width: float
) -> dict[str, list[float]]:
    """Return each stroke's local handwriting-run bounds.

    The pen stores a handwritten line as many small strokes. Treating each
    letter independently caused the 2026-09-01 page 3 sentence to become two
    YES marks. Nearby strokes on the same baseline form one run; if that run
    plainly extends through the answer row, every member is classified from
    the run rather than from one letter's small box.
    """
    records = []
    for index, stroke in enumerate(strokes):
        records.append(
            {
                "index": index,
                "id": str(stroke.get("stroke_id", f"stroke-{index + 1}")),
                "page": stroke.get("page"),
                "bbox": _bbox(stroke),
            }
        )
    parent = list(range(len(records)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(records):
        left_box = left["bbox"]
        left_cy = (left_box[1] + left_box[3]) / 2
        for right_index in range(left_index + 1, len(records)):
            right = records[right_index]
            if left["page"] != right["page"]:
                continue
            right_box = right["bbox"]
            right_cy = (right_box[1] + right_box[3]) / 2
            horizontal_gap = max(
                0.0,
                right_box[0] - left_box[2],
                left_box[0] - right_box[2],
            )
            if abs(left_cy - right_cy) <= box_width and horizontal_gap <= box_width:
                union(left_index, right_index)

    grouped: dict[int, list[list[float]]] = {}
    for index, record in enumerate(records):
        grouped.setdefault(find(index), []).append(record["bbox"])
    group_bounds = {
        root: [
            min(bounds[0] for bounds in members),
            min(bounds[1] for bounds in members),
            max(bounds[2] for bounds in members),
            max(bounds[3] for bounds in members),
        ]
        for root, members in grouped.items()
    }
    return {
        record["id"]: group_bounds[find(index)]
        for index, record in enumerate(records)
    }


def _run_crosses_box_row(
    run: list[float], boxes: list[dict[str, Any]]
) -> bool:
    row = [
        min(box["bbox"][0] for box in boxes),
        min(box["bbox"][1] for box in boxes),
        max(box["bbox"][2] for box in boxes),
        max(box["bbox"][3] for box in boxes),
    ]
    width = max(box["bbox"][2] - box["bbox"][0] for box in boxes)
    vertical_overlap = min(run[3], row[3]) >= max(run[1], row[1])
    outside = max(row[0] - run[0], run[2] - row[2], 0.0)
    return vertical_overlap and outside > width


def _page_carries_controls(page: int, entries: list[dict[str, Any]]) -> bool:
    """True when this page has an answer box or write-in line on it at all."""
    return any(entry.get("page") == page for entry in entries)


def _page_labels(manifest: dict[str, Any]) -> dict[int, str]:
    """One plain-English name per page, for naming a note in an alert."""
    labels: dict[int, str] = {}
    for card in manifest.get("cards", []):
        headline = str(card.get("headline") or card.get("card_id") or "")
        for page in card.get("page_numbers") or [card.get("page")]:
            if isinstance(page, int) and page not in labels:
                labels[page] = headline
    return labels


def _note_clusters(
    strokes: list[dict[str, Any]],
    note_ids: set[str],
    labels: dict[int, str],
    gap: float = NOTE_CLUSTER_GAP,
) -> list[dict[str, Any]]:
    """Group unclaimed ink into one region per piece of handwriting.

    mc-z5hy3: every stroke that no answer box and no write-in line claimed is
    note ink. It is grouped per page by proximity - two strokes join when
    their bounds are within `gap` of each other - so a sentence, or a
    paragraph, becomes one region with one crop rather than 130 opaque
    stroke ids in ignored_strokes.
    """
    records = [
        {
            "id": str(stroke.get("stroke_id", f"stroke-{index + 1}")),
            "page": stroke.get("page"),
            "bbox": _bbox(stroke),
        }
        for index, stroke in enumerate(strokes)
        if str(stroke.get("stroke_id", f"stroke-{index + 1}")) in note_ids
    ]
    parent = list(range(len(records)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(records):
        left_box = left["bbox"]
        for right_index in range(left_index + 1, len(records)):
            right = records[right_index]
            if left["page"] != right["page"]:
                continue
            right_box = right["bbox"]
            if (
                left_box[0] - gap <= right_box[2]
                and right_box[0] - gap <= left_box[2]
                and left_box[1] - gap <= right_box[3]
                and right_box[1] - gap <= left_box[3]
            ):
                union(left_index, right_index)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        grouped.setdefault(find(index), []).append(record)

    regions: list[dict[str, Any]] = []
    for members in grouped.values():
        boxes = [member["bbox"] for member in members]
        regions.append(
            {
                "page": members[0]["page"],
                "bbox": [
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    max(box[2] for box in boxes),
                    max(box[3] for box in boxes),
                ],
                "stroke_ids": sorted(member["id"] for member in members),
            }
        )
    regions.sort(key=lambda region: (region["page"], -region["bbox"][3], region["bbox"][0]))
    numbered: dict[int, int] = {}
    for region in regions:
        page = region["page"]
        numbered[page] = numbered.get(page, 0) + 1
        region["label"] = labels.get(page, f"page {page}")
        region["path"] = f"notes/page-{page:03d}-note-{numbered[page]}.png"
    return regions


def resolve_v3(manifest: dict[str, Any], strokes_doc: dict[str, Any]) -> dict[str, Any]:
    _validate_manifest(manifest)
    for key in ("schema_version", "layout_version", "page_id"):
        if strokes_doc.get(key) != manifest.get(key):
            raise tp.TodayPageError(
                f"Ink does not match this page ({key} differs). No action was taken."
            )
    strokes = strokes_doc.get("strokes")
    if not isinstance(strokes, list):
        raise tp.TodayPageError("Ink strokes are missing. No action was taken.")
    entries = manifest["entries"]
    entries_by_id = {entry["entry_id"]: entry for entry in entries}
    all_boxes = [
        (entry, box)
        for entry in entries
        for box in entry["boxes"]
    ]
    box_width = max(
        box["bbox"][2] - box["bbox"][0]
        for _, box in all_boxes
    )
    clusters = _stroke_clusters(strokes, box_width)
    hits: dict[str, set[str]] = {}
    box_evidence: dict[str, list[str]] = {}
    write_evidence: dict[str, list[str]] = {}
    ambiguities: list[dict[str, Any]] = []
    ambiguous_entries: set[str] = set()
    ignored: list[str] = []

    for index, stroke in enumerate(strokes):
        stroke_id = str(stroke.get("stroke_id", f"stroke-{index + 1}"))
        page = stroke.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= manifest["page_count"]:
            raise tp.TodayPageError(
                "Each stroke needs a valid 1-based page number. No action was taken."
            )
        bounds = _bbox(stroke)
        cx, cy = _centre(bounds)
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if width > tp.GIANT_MARK_SIZE or height > tp.GIANT_MARK_SIZE:
            # mc-z5hy3: on a page that carries no answer controls at all -
            # the cover, a Read appendix - an oversized mark cannot be a
            # spoiled tick, because there was nothing to tick. It is
            # handwriting, and on 2026-09-03 it was the underline in the reader's
            # cover note, so it falls through to the note sweep below.
            # On an answerable page the mark stays quarantined: there we
            # cannot tell a ruined answer from a note, and this pipeline
            # fails closed.
            if _page_carries_controls(page, entries):
                ambiguities.append(
                    {
                        "stroke_id": stroke_id,
                        "reason": "mark_too_large",
                        "bbox": bounds,
                        "page": page,
                    }
                )
            continue
        write_candidates = [
            entry
            for entry in entries
            if entry["kind"] == "decision"
            if entry["page"] == page
            and entry.get("write_in")
            and tp._inside(cx, cy, entry["write_in"]["bbox"])
        ]
        if len(write_candidates) > 1:
            candidates = sorted(entry["entry_id"] for entry in write_candidates)
            ambiguous_entries.update(candidates)
            ambiguities.append(
                {
                    "stroke_id": stroke_id,
                    "reason": "write_in_crosses_entries",
                    "bbox": bounds,
                    "page": page,
                    "candidate_entry_ids": candidates,
                }
            )
            continue
        if len(write_candidates) == 1:
            write_evidence.setdefault(write_candidates[0]["entry_id"], []).append(stroke_id)
            continue

        intersecting = [
            (entry, box)
            for entry, box in all_boxes
            if entry["page"] == page
            and tp._intersects(bounds, tp._expanded(box["bbox"]))
        ]
        raw_intersecting = [
            (entry, box)
            for entry, box in all_boxes
            if entry["page"] == page and tp._intersects(bounds, box["bbox"])
        ]
        centered = [
            (entry, box)
            for entry, box in intersecting
            if tp._inside(cx, cy, tp._expanded(box["bbox"]))
        ]
        center_raw_disagree = (
            len(centered) == 1
            and len(raw_intersecting) == 1
            and (centered[0][0]["entry_id"], centered[0][1]["choice"])
            != (
                raw_intersecting[0][0]["entry_id"],
                raw_intersecting[0][1]["choice"],
            )
        )
        crosses_boxes = (
            len(centered) > 1
            or len(raw_intersecting) > 1
            or center_raw_disagree
            or (not centered and len(intersecting) > 1)
        )
        if crosses_boxes:
            if center_raw_disagree:
                candidate_pairs = centered + raw_intersecting
            elif len(centered) > 1:
                candidate_pairs = centered
            elif len(raw_intersecting) > 1:
                candidate_pairs = raw_intersecting
            else:
                candidate_pairs = intersecting
            candidates = sorted({entry["entry_id"] for entry, _ in candidate_pairs})
            ambiguous_entries.update(candidates)
            ambiguities.append(
                {
                    "stroke_id": stroke_id,
                    "reason": "mark_crosses_boxes",
                    "bbox": bounds,
                    "page": page,
                    "candidate_entry_ids": candidates,
                }
            )
            continue
        if len(centered) != 1:
            ignored.append(stroke_id)
            continue
        entry, box = centered[0]
        if clusters[stroke_id] != bounds and _run_crosses_box_row(
            clusters[stroke_id], entry["boxes"]
        ):
            ignored.append(stroke_id)
            continue
        hits.setdefault(entry["entry_id"], set()).add(box["choice"])
        box_evidence.setdefault(entry["entry_id"], []).append(stroke_id)

    actions: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    write_ins: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = entry["entry_id"]
        choices = sorted(hits.get(entry_id, set()))
        if entry["kind"] == "action":
            if entry_id not in ambiguous_entries and choices == ["RUN"]:
                actions.append(
                    {
                        "entry_id": entry_id,
                        "label": entry["label"],
                        "page": entry["page"],
                        "choice": "RUN",
                        "stroke_ids": box_evidence.get(entry_id, []),
                        "action": entry["action"],
                    }
                )
            continue
        write_strokes = write_evidence.get(entry_id, [])
        choice = choices[0] if len(choices) == 1 else None
        if len(choices) > 1:
            ambiguities.append(
                {
                    "entry_id": entry_id,
                    "reason": "multiple_choices",
                    "choices": choices,
                    "page": entry["page"],
                    "label": entry["label"],
                    "candidate_entry_ids": [entry_id],
                }
            )
        if entry_id in ambiguous_entries or len(choices) > 1:
            status = "needs_review"
        elif choice in FINAL_BOXES and (len(choices) > 1 or write_strokes):
            status = "needs_review"
        elif choice == "DEFER":
            status = "deferred"
        elif choice == "DISCARD":
            status = "discarded"
        elif choice is not None:
            status = "answered"
        elif write_strokes:
            status = "answered"
        else:
            status = "unanswered"
        if len(choices) > 1:
            status = "needs_review"
        write_in = None
        if write_strokes:
            write_in = {
                "entry_id": entry_id,
                "path": f"write-ins/{entry_id}.png",
                "stroke_ids": write_strokes,
            }
            write_ins.append(write_in)
        decisions.append(
            {
                "entry_id": entry_id,
                "item_id": entry["item_id"],
                "card_id": entry["card_id"],
                "question_key": entry["question_key"],
                "decision_number": entry["decision_number"],
                "label": entry["label"],
                "answer_type": entry["answer_type"],
                "page": entry["page"],
                "status": status,
                "choice": choice,
                "marked_choices": choices,
                "stroke_ids": box_evidence.get(entry_id, []),
                "write_in": write_in,
            }
        )
    # mc-z5hy3: answers are spoken for; everything else on the page is the reader's
    # handwriting. Ink still held in quarantine is already surfaced as an
    # ambiguity, so it is not also claimed here.
    claimed = {
        stroke_id
        for evidence in (box_evidence, write_evidence)
        for stroke_ids in evidence.values()
        for stroke_id in stroke_ids
    }
    quarantined = {
        str(ambiguity["stroke_id"])
        for ambiguity in ambiguities
        if ambiguity.get("stroke_id")
    }
    note_ids = {
        str(stroke.get("stroke_id", f"stroke-{index + 1}"))
        for index, stroke in enumerate(strokes)
    } - claimed - quarantined
    note_regions = _note_clusters(strokes, note_ids, _page_labels(manifest))

    ink_hash = tp.sha256_bytes(tp.canonical_json(strokes).encode())
    return {
        "schema_version": SCHEMA_VERSION,
        "layout_version": LAYOUT_VERSION,
        "page_id": manifest["page_id"],
        "ink_hash": ink_hash,
        "status": "needs_review" if ambiguities or any(d["status"] == "needs_review" for d in decisions) else "resolved",
        "actions": actions,
        "decisions": decisions,
        "ambiguities": ambiguities,
        "ignored_strokes": ignored,
        "note_regions": note_regions,
        "write_ins": write_ins,
    }


def render_write_in_pngs(
    manifest: dict[str, Any],
    strokes_doc: dict[str, Any],
    resolution: dict[str, Any],
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Render write-in ink without interpreting its contents."""
    regions = resolution.get("write_ins", [])
    if not regions:
        return []
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise tp.TodayPageError("Write-in rendering needs Pillow.") from exc
    entries = {entry["entry_id"]: entry for entry in manifest["entries"]}
    strokes = {
        str(stroke.get("stroke_id")): stroke for stroke in strokes_doc.get("strokes", [])
    }
    rendered = []
    target_dir = Path(out_dir) / "write-ins"
    for region in regions:
        entry = entries[region["entry_id"]]
        x0, y0, x1, y1 = entry["write_in"]["bbox"]
        padding = tp.NOTE_PNG_PADDING
        scale = tp.NOTE_PNG_SCALE
        width = max(1, round((x1 - x0 + 2 * padding) * scale))
        height = max(1, round((y1 - y0 + 2 * padding) * scale))
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        for stroke_id in region["stroke_ids"]:
            stroke = strokes.get(stroke_id)
            if not stroke:
                continue
            pixels = [
                ((x - x0 + padding) * scale, (y1 - y + padding) * scale)
                for x, y in stroke["points"]
            ]
            if len(pixels) == 1:
                px, py = pixels[0]
                draw.ellipse([px - 2, py - 2, px + 2, py + 2], fill="black")
            else:
                draw.line(pixels, fill="black", width=3, joint="curve")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{region['entry_id']}.png"
        image.save(target, format="PNG")
        rendered.append({**region, "path": str(target)})
    return rendered
