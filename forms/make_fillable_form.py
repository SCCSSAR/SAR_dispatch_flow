#!/usr/bin/env python3
"""
make_fillable_form.py — Overlay AcroForm fields on the flat call-out form.

All field positions derived from actual PDF geometry (text bboxes + drawn rects).
Lives in forms/ alongside the source and output PDFs.

Run from repo root:
    /tmp/pdf_venv/bin/python3 forms/make_fillable_form.py

Venv setup (one-time):
    python3 -m venv /tmp/pdf_venv && /tmp/pdf_venv/bin/pip install pymupdf

Input/output default to the agency-neutral form that ships in forms/. Pass an
input and an output path to build a different pair:

    python3 forms/make_fillable_form.py "<flat form>.pdf" "<fillable out>.pdf"

The overlay geometry below is hardcoded to the v2 page layout, so a form it is
pointed at must share that layout below the title line.

Changes in this version:
  1. More vertical whitespace in the header box Row 1 (Date/Time row)
  2. "Last Seen" split into two fields: Date & Time (row 2a) + Location (row 2b)
  3. ☐☐ Unicode glyphs white-outed so only AcroForm checkboxes are visible
  4–7. Inline text fields added for Q9/Q10/Q11/Q12 detail notes
  8. "Missing Person Risk Factors" column header added to questionnaire
  9. ☐☐ whiteout also removes the spurious tab-stoppable pseudo-field
 10. Header box rows equally distributed — 5 rows evenly spaced in fixed box
 11. Labels "Last Seen Date & Time:" and "Last Seen Location:" vertically
     centered within their respective field bands (baselines at y=96 and y=114)
 12. "Last Seen Location:" replaces "Location:" — both Last Seen fields now
     start at x=172 so they have identical widths
 13. Inline detail text field added for Q5 ("Is the MP alone?")
"""

import sys

import fitz  # pymupdf
from pathlib import Path

INPUT  = Path(__file__).parent / "SAR Callout Form v2 (generic).pdf"
OUTPUT = Path(__file__).parent / "SAR Callout Form v2 (generic, fillable).pdf"

FONT      = "helv"
FONT_SIZE = 11
FILL   = (0.855, 0.937, 1.0)    # light blue — visible on screen, clean when printed
BORDER = (0.35, 0.35, 0.75)     # subtle blue-grey border


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

def tx(page, name, rect, multiline=False, tooltip=""):
    """Add a text field."""
    w = fitz.Widget()
    w.rect          = fitz.Rect(rect)
    w.field_type    = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name    = name
    w.field_value   = ""
    w.text_font     = FONT
    w.text_fontsize = FONT_SIZE
    w.text_color    = (0, 0, 0)
    w.fill_color    = FILL
    w.border_color  = BORDER
    w.border_width  = 1.0
    if multiline:
        w.field_flags = fitz.PDF_TX_FIELD_IS_MULTILINE
    if tooltip:
        w.field_label = tooltip
    page.add_widget(w)


def cb(page, name, rect, tooltip=""):
    """Add a checkbox."""
    w = fitz.Widget()
    w.rect         = fitz.Rect(rect)
    w.field_type   = fitz.PDF_WIDGET_TYPE_CHECKBOX
    w.field_name   = name
    w.field_value  = "Off"
    w.fill_color   = (1, 1, 1)
    w.border_color = (0.2, 0.2, 0.6)
    w.border_width = 1.0
    if tooltip:
        w.field_label = tooltip
    page.add_widget(w)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(page):

    # -----------------------------------------------------------------------
    # PHASE A — Content-stream modifications (shapes + text overlays).
    # These are rendered BELOW AcroForm annotations, so they safely white-out
    # original PDF content before widgets are placed on top.
    # -----------------------------------------------------------------------

    shape = page.new_shape()

    # === Fix 3 / Fix 9: White-out ☐☐ Unicode glyphs in questionnaire rows ===
    #
    # Original PDF has ☐☐ at x=72–126 in every questionnaire row.  These look
    # interactive in Preview (they ARE tab-stoppable as text objects) and they
    # create a "double checkbox" appearance on top of our AcroForm widgets.
    # Whiting-out just the interior of each YES/NO cell (inset 1pt from the
    # drawn borders at x=64/99.9/132.7) removes the glyphs while preserving
    # the visible cell grid lines.
    #
    # Row top edges (odd rows have drawn box outlines; even rows estimated):
    #   Q1:471.2  Q2:492.8  Q3:514.4  Q4:536.0  Q5:557.6  Q6:579.2
    #   Q7:600.8  Q8:629.4  Q9:651.3  Q10:672.9 Q11:694.5 Q12:716.1
    # Row height = 21.6pt (Q7 = 28.6pt — two-line question)

    ROW_TOPS = {
        1: 471.2, 2: 492.8, 3: 514.4,  4: 536.0,
        5: 557.6, 6: 579.2, 7: 600.8,  8: 629.4,
        9: 651.3, 10: 672.9, 11: 694.5, 12: 716.1,
    }

    for q_num, y0 in ROW_TOPS.items():
        rh = 28.6 if q_num == 7 else 21.6
        # YES cell interior (x=64.0–99.9, inset 1pt)
        shape.draw_rect(fitz.Rect(65.5, y0 + 1.0, 99.0, y0 + rh - 1.0))
        shape.finish(color=None, fill=(1, 1, 1))
        # NO cell interior (x=99.9–132.7, inset 1pt)
        shape.draw_rect(fitz.Rect(101.0, y0 + 1.0, 131.5, y0 + rh - 1.0))
        shape.finish(color=None, fill=(1, 1, 1))

    # === Fix 2 / Fix 10–12: White-out the entire gap between Row 1 and Row 3 ===
    #
    # Original label "Last Seen (time & location):" at x=45–203, y=94–106.
    # We replace it with two stacked labeled rows equally distributed in the
    # y=82–122 gap (between Row 1 bottom and Point of Contact top):
    #   "Last Seen Date & Time:" → labels field (y=86–100), baseline y=96
    #   "Last Seen Location:"    → labels field (y=104–118), baseline y=114
    shape.draw_rect(fitz.Rect(44, 82, 206, 122))
    shape.finish(color=None, fill=(1, 1, 1))

    shape.commit()   # write all shape content to PDF stream

    # === Fix 2 / Fix 10–12 (cont.): Insert replacement label text ===
    #
    # Both fields share x_start=172 so they have identical widths.
    # Label baselines are vertically centred in their field bands:
    #   Field 2a: y=86–100  → 14pt tall → baseline at y=86+11=97 ≈ 96
    #   Field 2b: y=104–118 → 14pt tall → baseline at y=104+11=115 ≈ 114
    # "Last Seen Date & Time:" at 9pt Helvetica Bold ≈ 124pt wide → ends x≈169.
    page.insert_text(
        fitz.Point(45, 96),
        "Last Seen Date & Time:",
        fontsize=9, fontname="hebo", color=(0, 0, 0),
    )
    # "Last Seen Location:" at 9pt bold ≈ 97pt wide → ends x≈142.
    # Field starts at x=172 matching the field above.
    page.insert_text(
        fitz.Point(45, 114),
        "Last Seen Location:",
        fontsize=9, fontname="hebo", color=(0, 0, 0),
    )

    # === Fix 8: "Missing Person Risk Factors" column header ===
    #
    # The questionnaire text-column header box is [132.7, 449.2, 572.8, 471.2].
    # Inserting the label at x=140, baseline y=463 centres it nicely.
    page.insert_text(
        fitz.Point(140, 463),
        "Missing Person Risk Factors",
        fontsize=9, fontname="hebo", color=(0, 0, 0),
    )

    # -----------------------------------------------------------------------
    # PHASE B — AcroForm widgets
    # -----------------------------------------------------------------------

    # =======================================================================
    # 1. HEADER BOX  (drawn rect=[35.5, 64.4, 576.5, 175.1])
    #
    # Fix 1 / Fix 10: Date/Time row at y=68–82, 4pt below box top (y=64.4).
    #
    # Fix 2 / Fix 10–12: "Last Seen" split into two equally-distributed rows.
    # The 40pt gap (y=82–122) between Row 1 and "Point of Contact" (y=122)
    # is divided with 4pt gutters:
    #   Row 2a (Date & Time):    y=86–100  label baseline y=96
    #   Row 2b (Last Seen Loc.): y=104–118 label baseline y=114
    # Both fields start at x=172 for identical widths (≈ same as row above).
    #
    # All label x-ranges confirmed from word extraction:
    #   "Date of Request:"                  ends x=140
    #   "Time of Request:"                  starts x=204, ends x=301
    #   "Last Seen Date & Time:" overlay    ends x≈169  → field x=172
    #   "Last Seen Location:"    overlay    ends x≈142  → field x=172
    #   "Point of Contact …"                ends x=302
    #   "Staging Area …"                    ends x=210
    # =======================================================================

    # Row 1 — Date (narrow: squeezed between two labels) and Time
    tx(page, "date_of_request",    (143, 68, 201, 82),
       tooltip="Date of Request  (e.g. 2/27/26)")
    tx(page, "time_of_request",    (304, 68, 572, 82),
       tooltip="Time of Request  (24-hr, e.g. 14:30)")

    # Row 2a — Last Seen Date & Time (label ends x≈169 → field x_start=172)
    tx(page, "last_seen_datetime", (172, 86, 572, 100),
       tooltip="Date and time MP was last seen  (e.g. 02/27/2026 14:30)")

    # Row 2b — Last Seen Location (label ends x≈142 → field x_start=172, same as row above)
    tx(page, "last_seen_location", (172, 104, 572, 118),
       tooltip="Last Known Position — full street address  (e.g. 123 Main St, San Jose, CA)")

    # Row 3 — Point of Contact (label at y=122–134; field aligned to label)
    tx(page, "point_of_contact",   (305, 122, 572, 136),
       tooltip="Officer/contact name and direct phone number")

    # Row 4 — Staging Area (label at y=150–162; field aligned to label)
    tx(page, "staging_area",       (213, 150, 572, 164),
       tooltip="Staging Area for Resources — where officer is waiting for SAR")

    # =======================================================================
    # 2. AGENCY BOX  (drawn rect=[35.5, 177.8, 576.5, 288.5])
    #
    # Inner boxes confirmed from path extraction:
    #   Agency inner box:   [96.8, 184.2, 199.2, 206.6]
    #   Event # inner box:  [296.5, 184.2, 567.9, 206.6]
    #   Request inner box:  [96.8, 213.6, 567.9, 282.2]
    # =======================================================================

    tx(page, "agency",       ( 98, 185, 198, 206),
       tooltip="Requesting agency (e.g. SJPD, MVPD, MILPITAS PD)")
    tx(page, "event_number", (298, 185, 566, 206),
       tooltip="Agency event / case number")
    tx(page, "request",      ( 98, 215, 566, 281), multiline=True,
       tooltip="Nature of request — describe situation, resources needed")

    # =======================================================================
    # 3. MISSING PERSON INFORMATION
    #    Outer box: [35.5, 341.2, 576.5, 744.8]
    #
    # Label y-ranges from word extraction:
    #   "Name:"             y=360–372
    #   "DOB:"              y=378–390
    #   "Address:"          y=396–408
    #   "Last Seen Wearing" y=414–426
    #   "Last Seen With"    y=432–444
    # =======================================================================

    tx(page, "mp_name",    ( 84, 359, 572, 373),
       tooltip="Missing person's full legal name")
    tx(page, "mp_dob",     ( 78, 377, 572, 391),
       tooltip="Date of birth (M/D/YYYY) and age  (e.g. 11/1/1965, age 60)")
    tx(page, "mp_address", (100, 395, 572, 409),
       tooltip="Full home address including city, state, zip")
    tx(page, "mp_wearing", (159, 413, 572, 427),
       tooltip="Clothing description: colors, jacket, shoes, etc.")
    tx(page, "mp_with",    (138, 431, 572, 445),
       tooltip="Companions, pets, or items the MP had with them")

    # =======================================================================
    # 4. QUESTIONNAIRE CHECKBOXES + INLINE TEXT FIELDS
    #
    # Fix 3: Checkboxes now span the full interior of each YES/NO drawn cell:
    #   YES cell drawn box: x=64.0–99.9  → widget x=66–98
    #   NO  cell drawn box: x=99.9–132.7 → widget x=101–130
    # Height 18pt (was 13pt); row height = 21.6pt so 2pt margin top+bottom.
    #
    # Inline text field x_start values from word extraction (end of last word):
    #   Q2  "Phone #:"   ends x=380 → start 382
    #   Q4  "Date:"      ends x=338 → start 342  (note: MUPS? ends x=273)
    #   Q5  "alone?"     ends x=212 → start 215  (Fix 13)
    #   Q6  "why?"       ends x=269 → start 273
    #   Q8  "languages:" ends x=343 → start 347
    # Fixes 4–7 (new fields for Q9–Q12):
    #   Q9  "Component?" ends x=260 → start 263
    #   Q10 "Missing?"   ends x=201 → start 204
    #   Q11 "checked?"   ends x=225 → start 228
    #   Q12 "CCTV?"      ends x=289 → start 292
    # =======================================================================

    INLINE = {
        2:  ("q2_phone",     382, "Phone number"),
        4:  ("q4_mups_date", 342, "MUPS entry date  (M/D/YY)"),
        5:  ("q5_details",   215, "Details if not alone  (e.g. who they're with, relationship)"),
        6:  ("q6_risk_why",  273, "Describe the risk  (e.g. early dementia, diabetic, suicidal)"),
        8:  ("q8_languages", 347, "Language(s) if not English"),
        9:  ("q9_details",   263, "Details  (e.g. diagnosis, current medications)"),
        10: ("q10_details",  204, "Details  (e.g. dates and locations of prior incidents)"),
        11: ("q11_details",  228, "Which hospitals were checked"),
        12: ("q12_details",  292, "Location / type  (e.g. 7-Eleven at Main & Oak)"),
    }

    for q_num, y0 in ROW_TOPS.items():
        rh = 28.6 if q_num == 7 else 21.6

        cb(page, f"q{q_num}_yes", (66,  y0 + 2, 98,  y0 + 20), tooltip=f"Q{q_num} — Yes")
        cb(page, f"q{q_num}_no",  (101, y0 + 2, 130, y0 + 20), tooltip=f"Q{q_num} — No")

        if q_num in INLINE:
            fname, x_start, tip = INLINE[q_num]
            tx(page, fname,
               (x_start, y0 + 2, 571, y0 + rh - 2),
               tooltip=tip)


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else INPUT
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT
    doc  = fitz.open(str(src))
    page = doc[0]
    build(page)
    doc.save(str(dst), garbage=4, deflate=True)
    print(f"Saved → {dst}")
    print(f"  Widgets total: {len(list(page.widgets()))}")
    print()
    for w in page.widgets():
        h = w.rect.y1 - w.rect.y0
        print(f"  {w.field_name:25s} {w.field_type_string:9s}  "
              f"y={w.rect.y0:.0f}–{w.rect.y1:.0f}  h={h:.0f}pt")


if __name__ == "__main__":
    main()
