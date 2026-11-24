"""
review-doc.py  --  render a design/review document to PDF for reMarkable

Template design goals:
  - Single column
  - Extra-wide right margin (~38% of page) for pen annotation
  - 1.5 line spacing
  - 12pt body (slightly smaller than brief -- design docs are denser)
  - Subtle annotation zone markers in right margin
  - Section headers with horizontal rules
  - Code blocks in Courier

Call render(markdown_text, output_path, title="") to produce the PDF.
Can also be run as a script: python3 review-doc.py input.md output.pdf [title]
"""

import re
import sys
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import black, HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT

PAGE_W, PAGE_H = letter

LEFT_MARGIN = 54
# Very wide right margin: 38% of page width minus some buffer
RIGHT_MARGIN = int(PAGE_W * 0.38)   # ~232 pt
TOP_MARGIN = 72
BOTTOM_MARGIN = 60

BODY_FONT_SIZE = 12
HEADING1_SIZE = 17
HEADING2_SIZE = 14
HEADING3_SIZE = 12
LINE_SPACING = 1.5 * BODY_FONT_SIZE


def _styles():
    styles = {}
    styles["body"] = ParagraphStyle(
        "body",
        fontName="Times-Roman",
        fontSize=BODY_FONT_SIZE,
        leading=LINE_SPACING,
        textColor=black,
        spaceAfter=7,
    )
    styles["h1"] = ParagraphStyle(
        "h1",
        fontName="Helvetica-Bold",
        fontSize=HEADING1_SIZE,
        leading=HEADING1_SIZE * 1.3,
        textColor=black,
        spaceBefore=20,
        spaceAfter=8,
    )
    styles["h2"] = ParagraphStyle(
        "h2",
        fontName="Helvetica-Bold",
        fontSize=HEADING2_SIZE,
        leading=HEADING2_SIZE * 1.3,
        textColor=HexColor("#111111"),
        spaceBefore=14,
        spaceAfter=6,
    )
    styles["h3"] = ParagraphStyle(
        "h3",
        fontName="Helvetica-BoldOblique",
        fontSize=HEADING3_SIZE,
        leading=HEADING3_SIZE * 1.3,
        textColor=HexColor("#333333"),
        spaceBefore=10,
        spaceAfter=4,
    )
    styles["bullet"] = ParagraphStyle(
        "bullet",
        parent=styles["body"],
        leftIndent=18,
        spaceAfter=3,
    )
    styles["code"] = ParagraphStyle(
        "code",
        fontName="Courier",
        fontSize=9,
        leading=12,
        textColor=HexColor("#111111"),
        backColor=HexColor("#F0F0F0"),
        leftIndent=10,
        rightIndent=10,
        spaceBefore=4,
        spaceAfter=4,
    )
    styles["title"] = ParagraphStyle(
        "title",
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=black,
        spaceAfter=6,
    )
    # Annotation prompts in right margin (not used in this simple renderer
    # but reserved for future enhancement)
    styles["annot_prompt"] = ParagraphStyle(
        "annot_prompt",
        fontName="Helvetica-Oblique",
        fontSize=8,
        textColor=HexColor("#AAAAAA"),
    )
    return styles


def _escape(text):
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _md_inline(text):
    # Extract code spans first and replace with placeholders to avoid
    # italic/bold regexes mangling underscores inside backtick spans.
    placeholders = {}
    idx = 0
    def stash_code(m):
        nonlocal idx
        key = f"\x00CODE{idx}\x00"
        idx += 1
        placeholders[key] = f"<font face='Courier'>{m.group(1)}</font>"
        return key
    text = re.sub(r"`(.+?)`", stash_code, text)

    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)

    for key, val in placeholders.items():
        text = text.replace(key, val)
    return text


def markdown_to_story(md_text, styles):
    story = []
    lines = md_text.split("\n")
    i = 0
    in_code_block = False
    code_lines = []
    para_buf = []

    def flush_para():
        if para_buf:
            content = _md_inline(_escape(" ".join(para_buf)))
            story.append(Paragraph(content, styles["body"]))
            para_buf.clear()

    bullet_buf = []

    def flush_bullet():
        if bullet_buf:
            content = _md_inline(_escape(" ".join(bullet_buf)))
            story.append(Paragraph(f"&bull;&nbsp;&nbsp;{content}", styles["bullet"]))
            bullet_buf.clear()

    def is_block_start(ln):
        return (ln.startswith("#") or
                re.match(r"^[-*] ", ln) or
                re.match(r"^\d+\. ", ln) or
                re.match(r"^---+$", ln.strip()) or
                ln.strip() == "" or
                ln.startswith("```"))

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            flush_para()
            flush_bullet()
            if in_code_block:
                in_code_block = False
                code_text = "<br/>".join(_escape(cl) for cl in code_lines)
                story.append(Paragraph(code_text or "&nbsp;", styles["code"]))
                code_lines = []
            else:
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            code_lines.append(line)
            i += 1
            continue

        if line.startswith("### "):
            flush_para()
            flush_bullet()
            story.append(Paragraph(_escape(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flush_para()
            flush_bullet()
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=HexColor("#CCCCCC"), spaceAfter=2))
            story.append(Paragraph(_escape(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            flush_para()
            flush_bullet()
            story.append(Paragraph(_escape(line[2:]), styles["h1"]))
        elif re.match(r"^[-*] ", line):
            flush_para()
            flush_bullet()
            bullet_buf.append(line[2:].strip())
        elif re.match(r"^\d+\. ", line):
            flush_para()
            flush_bullet()
            content = _md_inline(_escape(re.sub(r"^\d+\. ", "", line)))
            story.append(Paragraph(content, styles["bullet"]))
        elif re.match(r"^---+$", line.strip()):
            flush_para()
            flush_bullet()
            story.append(HRFlowable(width="100%", thickness=0.8,
                                    color=HexColor("#999999"),
                                    spaceBefore=6, spaceAfter=6))
        elif line.strip() == "":
            flush_para()
            flush_bullet()
            story.append(Spacer(1, 5))
        elif bullet_buf and not is_block_start(line):
            bullet_buf.append(line.strip())
        else:
            flush_bullet()
            para_buf.append(line.strip())

        i += 1

    flush_para()
    flush_bullet()
    return story


def render(md_text, output_path, title="Design Document"):
    styles = _styles()

    def add_header_footer(canv, doc):
        canv.saveState()

        # Annotation zone vertical line (right side)
        annotation_x = PAGE_W - RIGHT_MARGIN + 12
        canv.setStrokeColor(HexColor("#CCCCCC"))
        canv.setLineWidth(0.75)
        canv.line(annotation_x, BOTTOM_MARGIN + 15,
                  annotation_x, PAGE_H - TOP_MARGIN - 15)

        # Small "notes" label at top of annotation zone
        canv.setFont("Helvetica-Oblique", 7)
        canv.setFillColor(HexColor("#BBBBBB"))
        canv.drawString(annotation_x + 6, PAGE_H - TOP_MARGIN + 5, "notes")

        # Subtle horizontal tick marks in annotation zone every ~72pt
        canv.setLineWidth(0.3)
        for y_tick in range(int(BOTTOM_MARGIN + 40), int(PAGE_H - TOP_MARGIN - 30), 72):
            canv.line(annotation_x, y_tick, annotation_x + 8, y_tick)

        # Page number
        canv.setFont("Helvetica", 8)
        canv.setFillColor(HexColor("#888888"))
        canv.drawCentredString(PAGE_W / 2, 30, str(doc.page))

        # Running title header
        if title:
            canv.setFont("Helvetica", 8)
            canv.setFillColor(HexColor("#AAAAAA"))
            canv.drawString(LEFT_MARGIN, PAGE_H - TOP_MARGIN + 12,
                            title[:60])

        canv.restoreState()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=title,
    )

    story = []

    # Strip a leading H1 from the markdown if it matches the title
    # (avoids double-rendering when the caller passes title= explicitly)
    md_stripped = md_text
    first_h1 = re.match(r"^#\s+(.+?)$", md_text.lstrip(), re.MULTILINE)
    if title and first_h1 and first_h1.group(1).strip() == title.strip():
        md_stripped = md_text[first_h1.end():].lstrip("\n")

    if title:
        story.append(Spacer(1, 8))
        story.append(Paragraph(_escape(title), styles["title"]))
        story.append(HRFlowable(width="100%", thickness=2.0,
                                color=black, spaceAfter=14))

    story.extend(markdown_to_story(md_stripped, styles))

    doc.build(story, onFirstPage=add_header_footer, onLaterPages=add_header_footer)
    print(f"Written: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 review-doc.py input.md output.pdf [title]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]
    doc_title = sys.argv[3] if len(sys.argv) > 3 else ""

    with open(input_path) as f:
        md_text = f.read()

    if not doc_title:
        m = re.search(r"^# (.+)$", md_text, re.MULTILINE)
        doc_title = m.group(1) if m else "Design Document"

    render(md_text, output_path, doc_title)
