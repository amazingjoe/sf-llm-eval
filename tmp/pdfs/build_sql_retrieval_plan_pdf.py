from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "01-sql-retrieval-test-plan.md"
OUTPUT = ROOT / "output" / "pdf" / "01-sql-retrieval-test-plan.pdf"


def inline_markup(text: str) -> str:
    text = text.replace("—", "-").replace("–", "-").replace("‑", "-")
    value = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)


def footer(canvas, doc):
    canvas.saveState()
    width, _ = letter
    canvas.setStrokeColor(colors.HexColor("#d9e1ea"))
    canvas.line(0.7 * inch, 0.52 * inch, width - 0.7 * inch, 0.52 * inch)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.setFont("Helvetica", 8)
    canvas.drawString(0.7 * inch, 0.34 * inch, "01 SQL Retrieval Test Set - Working Plan")
    canvas.drawRightString(width - 0.7 * inch, 0.34 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    text = SOURCE.read_text(encoding="utf-8")

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="PlanTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=23, leading=27, textColor=colors.HexColor("#12304a"),
        spaceAfter=10, alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        name="PlanH2", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=15, leading=19, textColor=colors.HexColor("#0f6680"),
        spaceBefore=14, spaceAfter=7,
    ))
    styles.add(ParagraphStyle(
        name="PlanH3", parent=styles["Heading3"], fontName="Helvetica-Bold",
        fontSize=11.5, leading=14, textColor=colors.HexColor("#334155"),
        spaceBefore=10, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="PlanBody", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=9.2, leading=12.7, textColor=colors.HexColor("#1e293b"),
        spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="PlanBullet", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=9.0, leading=12.2, leftIndent=15, firstLineIndent=-10,
        bulletIndent=3, textColor=colors.HexColor("#1e293b"), spaceAfter=3,
    ))

    doc = BaseDocTemplate(
        str(OUTPUT), pagesize=letter, leftMargin=0.72 * inch, rightMargin=0.72 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="01 SQL Retrieval Test Set - Working Plan",
        author="Codex",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="plan", frames=[frame], onPage=footer)])

    story = []
    lines = text.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 3))
        elif stripped.startswith("# "):
            story.append(Paragraph(inline_markup(stripped[2:]), styles["PlanTitle"]))
        elif stripped.startswith("## "):
            story.append(KeepTogether([
                Spacer(1, 4),
                Paragraph(inline_markup(stripped[3:]), styles["PlanH2"]),
            ]))
        elif stripped.startswith("### "):
            story.append(Paragraph(inline_markup(stripped[4:]), styles["PlanH3"]))
        elif stripped.startswith("- "):
            story.append(Paragraph(inline_markup(stripped[2:]), styles["PlanBullet"], bulletText="-"))
        else:
            story.append(Paragraph(inline_markup(stripped), styles["PlanBody"]))

    doc.build(story)
    print(OUTPUT)


if __name__ == "__main__":
    build()
