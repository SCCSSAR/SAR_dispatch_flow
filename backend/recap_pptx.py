"""Write one month of the General Team incident recap as a new .pptx.

Needs python-pptx, which lives only in the recap venv (see .claude/skills/monthly-recap/SKILL.md).
Builds plain text boxes, tables and pictures, which import cleanly into Google Slides. Never
overwrites: the presenter edits the deck after it is generated, so an existing file is refused.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Inches, Pt

from monthly_recap import DRAFT, IncidentDraft, MonthDraft

ORANGE = "F1592A"
GREY = "9A9A9A"
INK = "1D1D1D"
HEADING_FONT = "Oswald"
BODY_FONT = "Public Sans"
W, H = Inches(13.333), Inches(7.5)

_CANCELLED_WORD = re.compile(r"\s*\bcancell?ed\b", re.IGNORECASE)


def _yes(value: str) -> bool:
    return "yes" in (value or "").lower()


def _real(lines: list[str]) -> list[str]:
    return [l for l in lines if l.strip() and l.strip() != DRAFT]


def _text(value: str) -> str:
    return "" if value.strip() == DRAFT else value.strip()


def _run(paragraph, text: str, *, font: str = BODY_FONT, size: int = 14, bold: bool = False,
         colour: str = INK, link: str | None = None):
    r = paragraph.add_run()
    r.text = text
    r.font.name, r.font.size, r.font.bold = font, Pt(size), bold
    r.font.color.rgb = RGBColor.from_string(colour)
    if link:
        r.hyperlink.address = link
    return r


def _box(slide, left, top, width, height):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tb.text_frame.word_wrap = True
    return tb.text_frame


def _bar(slide, colour: str) -> None:
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.18), H)
    bar.fill.solid()
    bar.fill.fore_color.rgb = RGBColor.from_string(colour)
    bar.line.fill.background()


def _title(slide, text: str) -> None:
    tf = _box(slide, Inches(0.5), Inches(0.3), Inches(12.3), Inches(0.8))
    _run(tf.paragraphs[0], text, font=HEADING_FONT, size=30, bold=True)


def _event(inc: IncidentDraft) -> str:
    return _CANCELLED_WORD.sub("", inc.facts.get("event") or inc.title).strip()


def _volunteer_total(vh: str) -> int | None:
    m = re.search(r"=\s*(\d+)\s*$", vh or "")
    return int(m.group(1)) if m else None


# ── summary slide ────────────────────────────────────────────────────────────

def _summary_slide(prs, draft: MonthDraft, overview_png: Path | None) -> None:
    s = prs.slides.add_slide(prs.slide_layouts[6])
    _bar(s, ORANGE)
    month = datetime.strptime(draft.month, "%Y-%m").strftime("%B %Y").upper()
    _title(s, f"{month} · INCIDENTS")

    rows, n, totals, open_hours, attendees = [], 0, 0, False, 0
    for inc in draft.incidents:
        ma, cancelled = _yes(inc.facts.get("mutual_aid", "")), _yes(inc.facts.get("cancelled", ""))
        if not ma:
            n += 1
        event = _event(inc) + (" — cancelled" if cancelled else "")
        vh = inc.facts.get("volunteer_hours", "")
        total = _volunteer_total(vh)
        if total is None:
            open_hours = True
        else:
            totals += total
        try:
            attendees += int(inc.facts.get("attendees", "0"))
        except ValueError:
            pass
        rows.append((["M/A" if ma else str(n), inc.facts.get("date", ""), event, vh,
                      inc.facts.get("attendees", "")], cancelled))
    n_cancelled = sum(c for _, c in rows)
    n_ma = sum(r[0] == "M/A" for r, _ in rows)
    footer = ["", "", f"{len(rows)} incidents ({n_cancelled} cancelled, {n_ma} M/A)",
              f"{totals}+" if open_hours else str(totals), str(attendees)]

    table_w = Inches(7.6) if overview_png else Inches(12.3)
    gt = s.shapes.add_table(len(rows) + 2, 5, Inches(0.5), Inches(1.3), table_w, Inches(0.4) * (len(rows) + 2))
    t = gt.table
    for i, w in enumerate((0.55, 0.75, 3.6, 2.0, 0.7)):
        t.columns[i].width = int(table_w * w / 7.6)
    all_rows = [(["#", "Date", "Event", "Volunteer hours", "Att."], "head")] + \
               [(r, "grey" if c else "body") for r, c in rows] + [(footer, "total")]
    for ri, (cells, kind) in enumerate(all_rows):
        for ci, value in enumerate(cells):
            cell = t.cell(ri, ci)
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor.from_string(ORANGE if kind == "head" else "FFFFFF")
            p = cell.text_frame.paragraphs[0]
            _run(p, value, font=HEADING_FONT if kind == "head" else BODY_FONT, size=12,
                 bold=kind in ("head", "total"),
                 colour="FFFFFF" if kind == "head" else (GREY if kind == "grey" else INK))
    if overview_png:
        s.shapes.add_picture(str(overview_png), Inches(8.4), Inches(1.3), height=Inches(5.6))


# ── incident slides ──────────────────────────────────────────────────────────

def _heading(tf, text: str) -> None:
    p = tf.add_paragraph()
    p.space_before = Pt(10)
    _run(p, text, font=HEADING_FONT, size=16, bold=True, colour=ORANGE)


def _bullets(tf, items: list[str]) -> None:
    for item in items:
        _run(tf.add_paragraph(), f"•  {item}", size=14)


def _incident_slide(prs, inc: IncidentDraft, map_png: Path | None, link: str | None) -> None:
    s = prs.slides.add_slide(prs.slide_layouts[6])
    cancelled, ma = _yes(inc.facts.get("cancelled", "")), _yes(inc.facts.get("mutual_aid", ""))
    _bar(s, GREY if cancelled else ORANGE)
    suffix = " — CANCELLED" if cancelled else (" · M/A" if ma else "")
    _title(s, f"{inc.facts.get('date', '')} · {_event(inc).upper()}{suffix}")

    tf = _box(s, Inches(0.5), Inches(1.2), Inches(6.3), Inches(6.0))
    summary = _text(inc.summary)
    _run(tf.paragraphs[0], summary, size=15)
    highlight = _text(inc.highlight)
    if highlight:
        p = tf.add_paragraph()
        p.space_before = Pt(6)
        _run(p, f"Highlight: {highlight}", size=15, bold=True)
    f = inc.facts
    chips = (["Cancelled"] if cancelled else [f.get("found", ""), ]) + [f.get("environment", "")]
    if not cancelled:
        chips.append(f.get("distance", ""))
    chips += [f.get("volunteer_hours", ""), f.get("specialty", ""), f"M/A: {'yes' if ma else 'no'}"]
    p = tf.add_paragraph()
    p.space_before = Pt(10)
    _run(p, "  ·  ".join(c for c in chips if c), size=12, colour="555555")
    if cancelled and _real(inc.disruption):
        _heading(tf, "THE DISRUPTION")
        _bullets(tf, _real(inc.disruption))
    if _real(inc.lessons):
        _heading(tf, "LESSONS LEARNED")
        _bullets(tf, _real(inc.lessons))
    _heading(tf, "TEAM TAKEAWAYS")

    left, top, width, height = Inches(7.1), Inches(1.2), Inches(5.8), Inches(4.35)
    if map_png:
        pic = s.shapes.add_picture(str(map_png), left, top, width=width)
        if link:
            pic.click_action.hyperlink.address = link
            cap = _box(s, left, top + pic.height + Inches(0.05), width, Inches(0.4))
            _run(cap.paragraphs[0], "Open the live map", size=12, colour=ORANGE, link=link)
    else:
        box = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
        box.fill.solid()
        box.fill.fore_color.rgb = RGBColor.from_string("EEEEEE")
        box.line.color.rgb = RGBColor.from_string(GREY)
        tf2 = box.text_frame
        _run(tf2.paragraphs[0], "Space for a photo", size=14, colour="777777")


def build_deck(draft: MonthDraft, overview_png: Path | None, maps: dict[str, Path | None],
               map_links: dict[str, str], out: Path) -> Path:
    if out.exists():
        raise FileExistsError(str(out))
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(W), Emu(H)
    _summary_slide(prs, draft, overview_png)
    for inc in draft.incidents:
        _incident_slide(prs, inc, maps.get(inc.number), map_links.get(inc.number))
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out
