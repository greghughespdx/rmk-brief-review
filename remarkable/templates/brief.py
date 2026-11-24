"""
brief.py  --  render a markdown brief to PDF for reMarkable reading

Template design goals:
  - Single column
  - 13pt body text (generous for e-ink)
  - 1.6 line spacing
  - Right margin 38% of page width for pen annotation
  - Page numbers
  - No decorative elements

Call render(markdown_text, output_path, title="") to produce the PDF.
Can also be run as a script: python3 brief.py input.md output.pdf
"""

import re
import sys
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import black, HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER

PAGE_W, PAGE_H = letter     # 612 x 792

# Wide right margin for annotation
LEFT_MARGIN = 54            # 0.75 inch
RIGHT_MARGIN = 200          # ~2.75 inch -- annotation space
TOP_MARGIN = 72
BOTTOM_MARGIN = 60

BODY_FONT_SIZE = 13
HEADING1_SIZE = 18
HEADING2_SIZE = 15
HEADING3_SIZE = 13
LINE_SPACING = 1.6 * BODY_FONT_SIZE


def _styles():
    base = getSampleStyleSheet()
    styles = {}

    styles["body"] = ParagraphStyle(
        "body",
        fontName="Times-Roman",
        fontSize=BODY_FONT_SIZE,
        leading=LINE_SPACING,
        textColor=black,
        spaceAfter=8,
    )
    styles["h1"] = ParagraphStyle(
        "h1",
        fontName="Times-Bold",
        fontSize=HEADING1_SIZE,
        leading=HEADING1_SIZE * 1.3,
        textColor=black,
        spaceBefore=18,
        spaceAfter=8,
        borderPad=(0, 0, 3, 0),
    )
    styles["h2"] = ParagraphStyle(
        "h2",
        fontName="Times-Bold",
        fontSize=HEADING2_SIZE,
        leading=HEADING2_SIZE * 1.3,
        textColor=black,
        spaceBefore=14,
        spaceAfter=6,
    )
    styles["h3"] = ParagraphStyle(
        "h3",
        fontName="Times-BoldItalic",
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
        firstLineIndent=0,
        spaceAfter=4,
    )
    styles["code"] = ParagraphStyle(
        "code",
        fontName="Courier",
        fontSize=10,
        leading=13,
        textColor=HexColor("#222222"),
        backColor=HexColor("#F5F5F5"),
        spaceBefore=4,
        spaceAfter=4,
        leftIndent=12,
        rightIndent=12,
    )
    styles["title"] = ParagraphStyle(
        "title",
        fontName="Times-Bold",
        fontSize=22,
        leading=26,
        textColor=black,
        spaceAfter=6,
        alignment=TA_LEFT,
    )
    styles["subtitle"] = ParagraphStyle(
        "subtitle",
        fontName="Times-Italic",
        fontSize=11,
        leading=14,
        textColor=HexColor("#555555"),
        spaceAfter=16,
    )
    return styles


def _escape(text):
    """Escape XML special chars for Paragraph."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _md_inline(text):
    """Convert basic markdown inline formatting to reportlab XML.

    Backtick spans are stashed first so underscores inside code spans
    don't trigger italic processing.
    """
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


def _is_block_element(line):
    """Return True if line starts a non-paragraph block (heading, bullet, rule, blank)."""
    return (line.startswith("#") or
            re.match(r"^[-*] ", line) or
            re.match(r"^\d+\. ", line) or
            re.match(r"^---+$", line.strip()) or
            line.strip() == "")


def markdown_to_story(md_text, styles):
    """Convert markdown text to a list of Platypus flowables.

    Consecutive non-blank, non-special lines are joined into a single paragraph
    so that source line-wrapping does not fragment the rendered text.
    """
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

    # list_buf accumulates continuation lines for multi-line list items
    # list_type: "bullet" or "numbered"
    list_buf = []
    list_type = None

    def flush_list():
        if list_buf:
            content = _md_inline(_escape(" ".join(list_buf)))
            if list_type == "bullet":
                story.append(Paragraph(f"&bull;&nbsp;&nbsp;{content}", styles["bullet"]))
            else:
                story.append(Paragraph(content, styles["bullet"]))
            list_buf.clear()

    # Keep old name as alias for compat
    bullet_buf = list_buf

    def flush_bullet():
        flush_list()

    def is_block_start(ln):
        return (ln.startswith("#") or
                re.match(r"^[-*] ", ln) or
                re.match(r"^\d+\. ", ln) or
                re.match(r"^---+$", ln.strip()) or
                ln.strip() == "" or
                ln.startswith("```"))

    while i < len(lines):
        line = lines[i]

        # Code block
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

        # Heading
        if line.startswith("### "):
            flush_para()
            flush_bullet()
            story.append(Paragraph(_escape(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flush_para()
            flush_bullet()
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=HexColor("#CCCCCC"), spaceAfter=4))
            story.append(Paragraph(_escape(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            flush_para()
            flush_bullet()
            story.append(Paragraph(_escape(line[2:]), styles["h1"]))
        # Bullet start
        elif re.match(r"^[-*] ", line):
            flush_para()
            flush_list()
            list_type = "bullet"
            list_buf.append(line[2:].strip())
        # Numbered list start
        elif re.match(r"^\d+\. ", line):
            flush_para()
            flush_list()
            list_type = "numbered"
            list_buf.append(re.sub(r"^\d+\. ", "", line).strip())
        # Horizontal rule
        elif re.match(r"^---+$", line.strip()):
            flush_para()
            flush_bullet()
            story.append(HRFlowable(width="100%", thickness=1.0,
                                    color=HexColor("#999999"), spaceBefore=6, spaceAfter=6))
        # Blank line = flush everything
        elif line.strip() == "":
            flush_para()
            flush_bullet()
            story.append(Spacer(1, 6))
        # Continuation of a list item
        elif list_buf and not is_block_start(line):
            list_buf.append(line.strip())
        # Normal paragraph text -- accumulate
        else:
            flush_list()
            para_buf.append(line.strip())

        i += 1

    flush_para()
    flush_list()
    return story


def render(md_text, output_path, title="Brief"):
    styles = _styles()

    def add_header_footer(canv, doc):
        canv.saveState()
        # Right margin annotation zone indicator (light grey vertical line)
        canv.setStrokeColor(HexColor("#DDDDDD"))
        canv.setLineWidth(0.5)
        annotation_x = PAGE_W - RIGHT_MARGIN + 10
        canv.line(annotation_x, BOTTOM_MARGIN + 20,
                  annotation_x, PAGE_H - TOP_MARGIN - 20)

        # Page number
        canv.setFont("Helvetica", 8)
        canv.setFillColor(HexColor("#888888"))
        canv.drawCentredString(PAGE_W / 2, 30, str(doc.page))
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

    md_stripped = md_text
    first_h1 = re.search(r"^#\s+(.+?)$", md_text.lstrip(), re.MULTILINE)
    if title and first_h1 and first_h1.group(1).strip() == title.strip():
        md_stripped = md_text[first_h1.end():].lstrip("\n")

    if title:
        story.append(Spacer(1, 12))
        story.append(Paragraph(_escape(title), styles["title"]))
        story.append(HRFlowable(width="100%", thickness=2.0, color=black, spaceAfter=12))

    story.extend(markdown_to_story(md_stripped, styles))

    doc.build(story, onFirstPage=add_header_footer, onLaterPages=add_header_footer)
    print(f"Written: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 brief.py input.md output.pdf [title]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]
    doc_title = sys.argv[3] if len(sys.argv) > 3 else ""

    with open(input_path) as f:
        md_text = f.read()

    if not doc_title:
        # Try to extract title from first H1
        m = re.search(r"^# (.+)$", md_text, re.MULTILINE)
        doc_title = m.group(1) if m else "Brief"

    render(md_text, output_path, doc_title)
