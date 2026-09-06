# The Call-Out Form

The pipeline is general. The form is not. This is the one file in the repo that has to
match your agency's paperwork before anything downstream works, so it ships as an
agency-neutral starting point rather than as somebody else's live document.

| File | What it is |
|---|---|
| `forms/SAR Callout Form v2 (generic).pdf` | The flat form — print it, hand it to a requesting agency |
| `forms/SAR Callout Form v2 (generic, fillable).pdf` | The same page with 47 AcroForm fields overlaid |
| `forms/make_fillable_form.py` | Rebuilds the fillable from the flat one |

The title line reads `[Agency Name]`. Replace it with yours.

---

## What the form is made of

Three parts, in page order. They do different jobs and are consumed by different code.

**1. Planning data** — date and time of request, last seen date/time and location, point of
contact, staging area, requesting agency, event number, and the free-text request.

This is the operational half. The last-seen location anchors geocoding, the staging area
becomes the command post marker, the event number becomes the D4H `trackingNumber`, and the
agency plus the last-seen street become the event name that every downstream system uses as
the incident's identity. Get these fields wrong and the whole dispatch points somewhere else.

**2. Subject data** — name, date of birth, home address, last seen wearing, last seen with.

Straight description, and the highest-sensitivity content on the page. Nothing here is
computed from; it is copied to the notification body, the incident channel, and the D4H
record.

**3. Risk and urgency criteria** — the twelve Yes/No questions.

These drive the lost-person-behavior analysis. Not all twelve carry equal weight:

| Question | Effect |
|---|---|
| Q6 (at-risk) + Q9 (mental health) | Select the subject category, which sets the range-ring distances |
| Q7 (proper equipment) | Feeds the at-risk factor list and the urgency read |
| Q5 (alone) | Feeds the at-risk factor list |
| Q1–Q4, Q8, Q10–Q12 | Case notes — read by the dispatcher, not computed from |

`docs/checkbox-accuracy-analysis.md` records how accurately each of these is read from a
photograph, and why the fillable PDF exists.

---

## Change these for your agency

**The title.** `[Agency Name]` is a placeholder.

**Q3 and Q4 use local vocabulary.** Q3 names *VTA*, the Santa Clara County transit
operator; Q4 names *MUPS*, California's Missing and Unidentified Persons System. Both are
asking a question every agency has — *can the subject move a long way by transit?* and *is
this person in the state missing-persons database?* — under a local name. Substitute yours
in the PDF, then in `_Q_DEFS` in `backend/pdf_extract.py` and in the prompt in
`backend/gemini.py`, or the extracted summary keeps saying VTA.

**Agency abbreviations.** `_AGENCY_DISPLAY` in `backend/main.py` canonicalizes the
requesting agencies this county sees. Yours will be different ones.

---

## Do not change these

**The literal `v2` in the title.** The prompt detects the form version by looking for it,
and v1 and v2 put the checkbox grid on opposite sides of the page. Remove the marker and a
v2 form is read as a v1 — which does not fail, it inverts answers.

**The 47 AcroForm field names.** `backend/pdf_extract.py` reads them by name. Renaming a
field silently drops it; the summary just says `[not recorded]`.

**Left column YES, right column NO.** Both the deterministic and the OCR path assume it.

**The fillable path itself, if you can help it.** A fillable PDF is read field by field
and is effectively perfect. A photograph of a filled-in paper form is read by a multimodal
model at roughly 8 of 12 checkboxes, with device-dependent variance, and no amount of prompt
engineering closes that gap. If you can get requesting agencies to send the fillable, that
single change buys more accuracy than anything you can do in code.

---

## Rebuilding the fillable

Edit the flat PDF in whatever tool you like, then re-overlay the fields:

```bash
python3 -m venv /tmp/pdf_venv && /tmp/pdf_venv/bin/pip install pymupdf
/tmp/pdf_venv/bin/python3 forms/make_fillable_form.py
```

Field positions in that script are hardcoded page coordinates, recovered from the geometry
of the flat PDF. If you move a label, move the matching rectangle — the script prints every
widget's name and position when it runs, which is the fastest way to check your work.

`backend/test_pdf_extract.py` reads the fillable PDF from the repo and asserts that all 24
checkbox fields and the 13 named text fields are present. Run the suite after any change to
the form:

```bash
python3 -m pytest -q backend/test_pdf_extract.py
```
