#!/usr/bin/env python3
"""Single source of truth for reMarkable document tag recognition.

WHY THIS FILE EXISTS (mc-ylap, 2026-08-16, after a the cross-reviewer BLOCK):

The tag matching used to be written twice inline in check-annotations.sh - once
in the `rmapi stat` parser for document-level tags, once in the `.content`
parser for page-level tags. The test suite substituted both of those call sites
at the network boundary and then reimplemented the normaliser a third time in
the tests themselves. The cross-reviewer proved the consequence mechanically: he changed
BOTH production comparisons to compare against 'nevermatch' and the entire
suite still passed, banner included. Nothing under test was the code that runs.

One helper, imported by the tests and invoked by the shell script, is what makes
the matcher falsifiable. If you are tempted to inline a tag comparison anywhere
else, put it here instead.

The reader's two tags (pen-dictated 2026-08-15):
  ready         his done-annotating signal; the doc carries markup to PROCESS.
  Mark-as-Read  his READ RECEIPT; he opened and finished reading it. Automation
                never sets it and never clears it, and it never triggers
                processing on its own.

Live spelling, observed 2026-09-09 (mc-1gx9o): the tag the reader actually sets on
the device reads "Marked as Read" (rmapi stat on /Briefs/a10m-vs-gpu2-2026-09-04,
set 2026-09-04). Until that day the matcher accepted only the dictated
"Mark-as-Read" family, so every receipt he had set was invisible. `normalise`
strips case and every non-alphanumeric character before comparing, and the
comparison accepts both markedasread and markasread, so Marked as Read,
marked_as_read, Mark-as-Read, mark_as_read and "Mark As Read" all count. Near
misses (read, unread, marked as unread, mark-as-read-later) do not.
"""

from __future__ import annotations

import json
import re
import sys

READY = "ready"
# The reader's real read-receipt tag, as first observed on the device 2026-09-09 on
# /Briefs/a10m-vs-gpu2-2026-09-04 (set 2026-09-04): "Marked as Read". The
# dictated form "Mark-as-Read" is kept too. Both normalise to these (mc-1gx9o).
SEEN = frozenset({"markasread", "markedasread"})


def normalise(name) -> str:
    """Lowercase and drop every non-alphanumeric character."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def is_ready(name) -> bool:
    """A 'ready' tag. Matched case-insensitively but NOT normalised: 'ready' has
    no separators to vary, and loosening it would let 're-ady' or 'READY!' in."""
    return str(name).lower() == READY


def is_seen(name) -> bool:
    """A mark-as-read tag ("Marked as Read" or "Mark-as-Read"), in any case or
    separator form."""
    return normalise(name) in SEEN


def flags(names) -> tuple[str, str]:
    """(ready, seen) as 'y'/'n' for an iterable of tag names."""
    names = list(names or [])
    return (
        "y" if any(is_ready(n) for n in names) else "n",
        "y" if any(is_seen(n) for n in names) else "n",
    )


def _mapping(value) -> dict:
    """A dict, or an empty one. Valid JSON is not the same as expected JSON."""
    return value if isinstance(value, dict) else {}


def _sequence(value) -> list:
    """A list, or an empty one. Strings are deliberately NOT iterated: a bare
    string would otherwise decompose into characters and match nothing, which
    silently answers a question we could not actually answer."""
    return value if isinstance(value, (list, tuple)) else []


def _tag_name(item) -> str:
    """A tag name from either shape seen in the wild: {"name": "ready"} in
    .content payloads, or a bare string in Tags arrays."""
    if isinstance(item, dict):
        name = item.get("name", "")
        return name if isinstance(name, str) else ""
    return item if isinstance(item, str) else ""


def stat_names(doc):
    """Tag names from an `rmapi stat` payload (document level)."""
    return [_tag_name(t) for t in _sequence(_mapping(doc).get("Tags"))]


def content_names(doc):
    """Tag names from a `.content` payload (document and page level)."""
    doc = _mapping(doc)
    names = [_tag_name(t) for t in _sequence(doc.get("tags"))]
    names += [_tag_name(t) for t in _sequence(doc.get("pageTags"))]
    return names


def lsjson_names(entry):
    """Tag names from one entry of an `rmapi -json ls` array.

    Different key case from stat_names on purpose. `rmapi stat` marshals
    model.Document, which has no struct tags, so Go emits PascalCase
    ("Tags", "ModifiedClient"). `-json ls` and `-json find` go through
    shell/output.go's NodeJSON, which DOES carry struct tags, so the same
    fields arrive lowerCamelCase ("tags", "modifiedClient"). Both shapes are
    real and both are ours to read. Verified against rmapi v0.0.35 source and
    against the struct tags compiled into the installed binary (mc-rsh8).
    """
    return [_tag_name(t) for t in _sequence(_mapping(entry).get("tags"))]


def lsjson_rows(payload, dirpath):
    """Stage-1 rows for every document in an `rmapi -json ls <dirpath>` array.

    Yields "<path>\\t<ModifiedClient>\\t<ready>\\t<seen>", the exact shape
    stage 1 already builds from find plus a stat per file, so the state
    machine downstream cannot tell which scanner produced it.

    NodeJSON carries `name` but not a full path, so the caller supplies the
    directory it listed and the path is rebuilt here. That is why this is an
    `ls` collapse and not a `find` collapse: `-json find` walks recursively
    but flattens every entry to a bare name, and rebuilding paths from its
    `parent` UUIDs would be a second, guessable failure mode.

    Directories and templates are skipped: only DocumentType is a document.
    An entry with no usable modifiedClient becomes the __unreadable__ marker
    rather than vanishing, because a document silently missing from stage 1
    gets its stored row deleted and can later look brand new (mc-ylap R3).
    """
    dirpath = str(dirpath or "").rstrip("/")
    for entry in _sequence(payload):
        entry = _mapping(entry)
        if entry.get("type") != "DocumentType":
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        path = f"{dirpath}/{name}"
        mod = entry.get("modifiedClient", "")
        if not isinstance(mod, str) or not mod:
            yield f"{path}\t__unreadable__\tn\tn"
            continue
        ready, seen = flags(lsjson_names(entry))
        yield f"{path}\t{mod}\t{ready}\t{seen}"


def _soft_fail(mode) -> str:
    """What each mode prints when the payload is unusable.

    stat prints an empty line (no mtime), content prints the untagged
    answer, and lsjson prints NOTHING AT ALL - not an empty folder, not a
    row. An empty lsjson scan means "the listing failed", and the caller
    must be able to tell that apart from "this folder has no documents",
    because treating a failed listing as an empty folder would delete
    every stored row under it.
    """
    if mode == "stat":
        return "\n"
    if mode == "content":
        return "n|n\n"
    return ""


def main(argv) -> int:
    """Read JSON on stdin, print the flags check-annotations.sh expects.

    stat    -> "<ModifiedClient>\\t<ready>\\t<seen>", or empty when no mtime
    content -> "<ready>|<seen>"

    Both modes fail SOFT and the boundary covers SHAPE, not just syntax. That
    distinction is load-bearing (the cross-reviewer, mc-ylap R3): an earlier version caught
    only json.load errors, so valid-but-unexpected JSON - a top-level list, a
    null, a Tags array of bare strings - escaped and exited non-zero with no
    output. Stage 1 runs this in command substitution under `set -u` and not
    `set -e`, so the watcher did not fail; the document simply produced no line,
    vanished from the new state table, and had its stored row DELETED when the
    state file was replaced. On a later healthy run it would look brand new and
    could fire a false transition. Silence from this helper is never harmless,
    so nothing may raise past here.

    A parse failure must not be able to look like a tag, and it must not be
    able to look like an answer either.
    """
    mode = argv[1] if len(argv) > 1 else ""
    try:
        doc = json.load(sys.stdin)
    except Exception:
        print(_soft_fail(mode), end="")
        return 0

    try:
        if mode == "stat":
            mod = _mapping(doc).get("ModifiedClient", "")
            if not isinstance(mod, str) or not mod:
                print("")
                return 0
            ready, seen = flags(stat_names(doc))
            print(f"{mod}\t{ready}\t{seen}")
            return 0

        if mode == "content":
            ready, seen = flags(content_names(doc))
            print(f"{ready}|{seen}")
            return 0

        if mode == "lsjson":
            # Same fail-soft contract as the other modes: a payload we cannot
            # make sense of prints nothing and exits 0, and the caller treats
            # an empty scan as a failed listing rather than as an empty folder.
            for row in lsjson_rows(doc, argv[2] if len(argv) > 2 else ""):
                print(row)
            return 0
    except Exception:
        # Belt and braces. The coercions above should make this unreachable;
        # if it is ever reached, the untagged answer is still the safe one.
        print(_soft_fail(mode), end="")
        return 0

    print("usage: tag_flags.py {stat|content} < payload.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
