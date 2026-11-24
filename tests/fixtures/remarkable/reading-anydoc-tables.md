# Reading fixture: tables, links and inline markers

Passages from a research note about a public open-source project, the kind of
document whose reading pages once printed raw pipes, backticks and bracketed
links. Kept here so the renderer is tested against real shapes, not only
against invented ones.

## A three-column table that fits the page

| Fact | Value | How verified |
|---|---|---|
| Licence | MIT, Copyright 2026 Sideguide Technologies Inc | `LICENSE` in the repo |
| Language | Rust, edition 2024, requires rustc 1.88 | `Cargo.toml` |
| Version | 0.2.4, released 2026-08-27 | `Cargo.toml`, `gh release list` |
| First commit | 2026-07-30 | `git log --reverse` |
| Commits | 130 | `git rev-list --count HEAD` |
| Last push | 2026-08-28 | GitHub API |
| Stars / forks | 20,117 / 1,215 | GitHub API, read 2026-09-03 |
| Open issues | 83 | GitHub API |
| Contributors | the lead author 118 commits, two others 6 and 4, two more 1 each | GitHub API |

## The five-column head-to-head the reader most needs

| Document | Size | anydoc | MarkItDown | Notes |
|---|---|---|---|---|
| `Procedures manual.docx` (a real procedures manual) | 304 KB | **97 ms**, 225 KB out | 3,588 ms, 224 KB out | 37x faster, same content volume |
| `SOP matching example.docx` | 28 KB | **50 ms**, 19.6 KB | 815 ms, 19.6 KB | 16x faster |
| `executive-briefing-draft-1.pptx` | 52 KB | **55 ms**, 6.3 KB | 825 ms, 6.5 KB | 15x faster, but see the slide-boundary problem below |
| `2026-coordinator-guide.pdf` | 464 KB | **1,211 ms**, 27 KB | 1,916 ms, 26.9 KB | anydoc found 13 headings and 3 table rows; MarkItDown found zero of either |
| `Raw Data.xlsx` (a flight-data workbook) | 7.0 MB | **FAILED** at 1,324 ms | 59,976 ms, 10.8 MB out | see below |
| 14-byte stub named `.pdf` (uploads fixture) | 14 B | rejected: "not a PDF: invalid PDF file header" | emitted the raw bytes as content | anydoc fails loudly, MarkItDown emits garbage silently |

## Links inside a paragraph

anydoc emits no slide separator at all, so the briefing decks run together as one
stream of text. Upstream issue
[#94](https://github.com/firecrawl/anydoc/issues/94) says the anchor exists in the
model and is simply not exposed yet.

Two open issues ([#157](https://github.com/firecrawl/anydoc/issues/157) and
[#146](https://github.com/firecrawl/anydoc/issues/146)) ask for a local OCR provider
hook. The plain address https://github.com/firecrawl/anydoc is written bare on
purpose: a bare URL is already readable and must be left alone.

There is an open upstream issue for exactly this,
[#156 "xlsx: resource limits hit on real workbooks with excess whitespace"]
(https://github.com/firecrawl/anydoc/issues/156), filed 2026-09-01. That link is
wrapped across a source line break exactly as the research document wraps it.

## Inline markers that must not print

The parsers return `Result` rather than panicking, the caps live in
`src/package/limits.rs`, the timing above is **97 ms** against *3,588 ms*, and the
identifier close_ticket keeps both of its underscores while _this phrase_ and
__that phrase__ lose their markers.
