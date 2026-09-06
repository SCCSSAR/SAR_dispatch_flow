# SCCSSAR Dispatch Console — Checkbox Accuracy Analysis & Decisions
*Session: Feb 2026*

## Background

The SCCSSAR call-out form contains 12 Yes/No checkbox questions (Q1–Q12) in the "Missing Person (MP) Information" section. Gemini multimodal OCR reads them at approximately 8/12 accuracy with device-dependent variance (iPad consistently wrong on Q1, Q8, Q11). This document records what was investigated, what was decided, and why — so future sessions don't re-litigate closed questions.

---

## What Q1–Q12 Actually Drives

Not all checkbox errors have equal impact. Understanding this shapes the mitigation strategy.

### Computationally significant (wrong answer changes LPB output)
- **Q6** (At-risk) + **Q9** (Mental health component) → Koester subject category → range ring radii on CalTopo map
- **Q7** (Proper equipment) → at-risk factor list, potentially influences Koester Local Modifiers
- **Q5** (Alone) → at-risk factor list

### Pure case notes (dispatcher reads, nothing computed)
- Q1 (Familiar with area)
- Q2 (Has phone) — phone number is in freeform text, reliably extracted
- Q3 (Uses VTA)
- Q4 (MUPS) — date is in freeform text, reliably extracted
- Q8 (Speaks English) — language appears in freeform text
- Q10 (Prior missing) — narrative in freeform text
- Q11 (Hospitals checked) — dispatch coordination note
- Q12 (Surveillance cameras) — investigation note

**Practical implication:** Gemini already reads the freeform text next to each checkbox reliably (confirmed by Document AI testing — DocAI extracted Q6/Q9 freeform text accurately even when it failed completely on the checkboxes). The checkbox Yes/No answer is additive context; the freeform text carries the operational content.

---

## Investigation: Google Document AI Form Parser

### Test conducted
- 13 labeled SCCSSAR call-out forms tested against Document AI Form Parser v2.0 (stable)
- Ground truth: manually reviewed answers in `sample_forms_Q1-12.csv`
- Processor: `projects/970461953836/locations/us/processors/1dcc2ef78b67b09a`
- Test script: `/tmp/docai_test.py`

### Result
**Score: 0/12 correct across all 13 forms.**

### Root cause
Form Parser treats the SCCSSAR form as a generic key-value document. It cannot associate the narrow two-column YES/NO checkbox grid with individual question rows. The checkbox tokens (☑/☐) are detected individually but have no row-level anchoring — it's a fundamental layout comprehension failure, not a character recognition issue.

Specific failure modes observed:
- Most Q1–Q12 fields returned as NOT FOUND (checkbox columns not mapped to questions at all)
- When a question was found (Q2, Q4, Q5, Q8, Q9), the *value* returned was the freeform text answer, not the checkbox state
- Q11 key paired with Q10 value text — adjacent fields contaminating each other
- Two forms showed raw `'YES': '☑'` and `'NO': '☐\n☐'` detection — correct data, completely unanchored from which question it belongs to

### Decision
**Do not integrate Document AI Form Parser.** The test is conclusive; do not re-investigate.

The Form Parser processor remains enabled in `sar-dispatch-dev` (processor ID `1dcc2ef78b67b09a`) at negligible cost — it may be useful for extracting other structured fields (Event Number, Agency, Date of Request) in future.

---

## Investigation: Custom Document Extractor (CDE)

### What it would require
- Label 50–100 forms in Document AI Workbench (current corpus: 13)
- Train a custom model specific to the SCCSSAR form layout
- New `backend/docai.py`, IAM changes, Terraform changes, merge logic in `main.py`
- Re-train whenever the form is redesigned

### Estimated accuracy
~75–80% on 13 training examples; ~90%+ with 50+ examples.

### Decision
**Do not pursue Custom Extractor at this time.** Reasoning:

1. **Dominated by form redesign option.** A redesigned form (question number adjacent to checkbox pair) gives Gemini a spatial anchor and likely gets accuracy to 90%+ with zero backend changes. CDE training investment becomes wasted if the form changes.
2. **Marginal improvement over current state.** 75–80% on 13 examples is not meaningfully better than Gemini's ~67%, at high effort cost.
3. **Re-evaluate if:** form redesign is ruled out AND the labeled corpus grows to 50+ forms AND checkbox errors are causing operational harm (not just amber-banner warnings).

If CDE is ever revisited, train it on the *redesigned* form layout, not the current one.

---

## Investigation: Structured Q1–Q12 Correction UI with LPB Re-compute

### Proposal
Add 12 toggle controls (Yes/No/Not Answered) to the review screen, pre-populated from Gemini's output. When the dispatcher corrects a checkbox answer, trigger a server-side Pass 3 to re-run the Koester LPB section with corrected inputs.

### Decision
**Do not implement at this time.** Reasoning:

1. **Latency is already a pain point.** Form processing takes ~45–60 seconds during a stressful active incident. Adding another Gemini round-trip for correction degrades the experience further.
2. **Range rings are used infrequently.** Dispatchers manually delete or adjust CalTopo map rings when needed. The marginal value of auto-corrected range rings does not justify the latency cost.
3. **The freeform text is already correct.** Gemini reliably reads Q6/Q9 freeform text (diagnosis, at-risk reason), which is the operationally critical content. A wrong checkbox on Q9 with the correct "DEMENTIA" freeform text still produces the right Koester category.
4. **Dispatcher already verifies.** The amber warning banner prompts visual verification of all Q1–Q12 against the original photo. Extending that to "verify the Koester category label makes sense" is a reasonable ask.

**Accepted mitigation:** Gemini ~8/12 checkbox accuracy + amber warning banner + dispatcher visual verification. This is the ceiling for the current form layout.

---

## Parallel Path: Form Redesign for Better Parsability

Two options under exploration (Bill working in parallel):

**Option A — Question number adjacent to checkbox pair, then question text + freeform field**
Strongest fix. Gives Gemini a row-level spatial anchor — number, box pair, and question text on the same line. Eliminates the vertical-trace ambiguity that causes Q1/Q8/Q11 errors. Estimated accuracy improvement: to ~90%+. Officer retraining: minimal (layout change, not workflow change).

**Option B — Duplicate question number on right side of checkbox columns**
Lighter change. Zero officer retraining. Helps Gemini with row identification but the two-column YES/NO ambiguity within the pair remains. Estimated improvement: ~80–85%.

**Recommendation:** Prototype both, test with photos of a filled-out redesigned form, measure Gemini accuracy improvement before committing to a print run.

---

## Long-Term Architecture: Online Intake Form

The correct end-state is an online intake form that officers fill out digitally. This eliminates the OCR/checkbox problem entirely — structured data arrives at the backend without image interpretation. Logged as a future backlog item; blocked on officer workflow adoption and device availability in the field.

---

## Current Accepted State (as of Feb 2026)

| Question | Checkbox accuracy | Freeform accuracy | Operational impact of error |
|----------|------------------|-------------------|-----------------------------|
| Q1 | ~67%, device-variant | N/A | Low — case note only |
| Q2 | ~67% | High (phone # extracted) | Low — freeform carries the number |
| Q3 | ~67% | N/A | Low — case note only |
| Q4 | ~67% | High (date extracted) | Low — freeform carries the date |
| Q5 | ~67% | N/A | Medium — affects at-risk list |
| Q6 | ~67% | High (diagnosis extracted) | Medium — Koester, but freeform compensates |
| Q7 | ~67% | Medium | Medium — affects at-risk list |
| Q8 | ~67%, device-variant | High (language extracted) | Low — freeform carries the language |
| Q9 | ~67% | High (diagnosis extracted) | Medium — Koester, but freeform compensates |
| Q10 | ~67% | High (narrative extracted) | Low — case note only |
| Q11 | ~67%, device-variant | Medium | Low — dispatcher verifies directly |
| Q12 | ~67% | Medium | Low — case note only |

**Net assessment:** The checkbox accuracy problem is real but its operational impact is lower than the raw 67% number suggests, because the freeform text next to each checkbox reliably carries the substantive content. The cases most at risk (Q6/Q9 → Koester) are also the cases where Gemini's freeform extraction is most reliable (diagnosis names are consistently read correctly).
