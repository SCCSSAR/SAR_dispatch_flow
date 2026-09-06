"""
pdf_extract.py — AcroForm field extraction and synthetic summary builder for v2 PDF forms.

DESIGN DECISION (do not revert without team discussion): The PDF path is a separate
accuracy track from the JPEG OCR path. Checkbox answers are read deterministically
from AcroForm field values — 100% accurate, no Gemini OCR involvement.

Privacy: PDF bytes are processed entirely in-memory. No bytes are written to disk,
GCS, Firestore, or logs. pymupdf opens from a bytes stream, not a file path.

Field schema (47 fields confirmed from forms/SAR Callout Form v2 (generic, fillable).pdf):
  Header:    date_of_request, time_of_request, last_seen_datetime, last_seen_location,
             point_of_contact, staging_area
  Agency:    agency, event_number, request
  MP:        mp_name, mp_dob, mp_address, mp_wearing, mp_with
  Checkboxes: q1_yes/q1_no … q12_yes/q12_no  (24 fields)
  Detail text: q2_phone, q4_mups_date, q5_details, q6_risk_why, q8_languages,
               q9_details, q10_details, q11_details, q12_details  (9 fields)
"""

import logging
import re
import zoneinfo
from datetime import date as _date, datetime
from typing import Optional

import fitz  # pymupdf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PDF_MAGIC = b"%PDF"

# Maximum size for a PDF upload — same limit as JPEG path.
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB

# Checkbox field value when checked (pymupdf convention).
_CHECKED = "Yes"
_UNCHECKED = "Off"

# Q question labels — matches existing output format in SYSTEM_PROMPT exactly.
# Tuples of (base_label, optional_detail_field_name, optional_detail_prefix).
_Q_DEFS: dict[int, tuple[str, Optional[str], Optional[str]]] = {
    1:  ("Familiar with area",                              None,           None),
    2:  ("Has phone",                                       "q2_phone",     "number"),
    3:  ("Uses public transit (VTA)",                       None,           None),
    4:  ("Entered into MUPS",                               "q4_mups_date", "date"),
    5:  ("Alone",                                           "q5_details",   "detail"),
    6:  ("At-risk",                                         "q6_risk_why",  "reason"),
    7:  ("Proper equipment (hiking gear, cold-weather gear, etc.)", None,   None),
    8:  ("Speaks English",                                  "q8_languages", "languages"),
    9:  ("Mental health component",                         "q9_details",   "detail"),
    10: ("Prior missing",                                   "q10_details",  "detail"),
    11: ("Hospitals checked",                               "q11_details",  "detail"),
    12: ("Surveillance cameras / CCTV",                     "q12_details",  "detail"),
}

NOT_ANSWERED = "NOT ANSWERED (flag for follow-up)"

# Issue #774 — a bare AGE typed into the DOB field. Normal on mutual aid, where
# the intake is transcribed from a request email and the requesting agency
# supplied an age rather than a date of birth. Live 2026-08-23: the AcroForm
# value was "24 yo", nothing downstream could read it, and the D4H Involved tab
# came out blank.
#
# Optional unit, because the observed shapes vary ("24 yo", "24yo", "24 y/o",
# "24 years old", "24"). A birth YEAR can never be read as an age: "1995",
# "2005" and "150" all fail the _MAX_PLAUSIBLE_AGE range check, which is the
# guarantee. The 3-digit cap in the pattern is a cheap first filter on top of
# it and carries nothing on its own. The residual ambiguity is a 2-digit year
# shorthand —
# "95" meaning 1995 reads here as age 95. Accepted knowingly: this branch only
# runs after _to_iso_date() has already declined the value, so the alternative
# is the status quo of dropping the field silently, and reading "95" as a date
# would require inventing a century AND a month AND a day, which is far more
# inference than reading the number in an age-bearing field as an age.
# The trailing `(...)` is not decoration: the real corpus carries
# "84 YEARS OLD (DOB UNKNOWN)", which is this exact situation stated twice —
# the agency gave an age BECAUSE it had no date of birth. Rejecting it for the
# parenthetical would decline the clearest example of the case.
_BARE_AGE_RE = re.compile(
    r"^(?:age\s*[:\-]?\s*)?(\d{1,3})\s*(?:y/?o|yo|yrs?|years?(?:\s+old)?)?\.?"
    r"(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
_MAX_PLAUSIBLE_AGE = 120

# An age hint the value already carries, in the PARENTHESIZED form every
# downstream consumer keys on (main.py::_D4H_RE_DOB, main.py::_DOB_AGE_HINT_RE,
# index.html::_ebParseAgeFromDob). Load-bearing only because the pattern above
# admits a trailing parenthetical: without this, "24 (24 years old)" matches
# and earns a second hint.
_EXISTING_AGE_HINT_RE = re.compile(r"\(\s*\d+\s*years?\s+old\s*\)", re.IGNORECASE)


def _bare_age(raw: str) -> Optional[int]:
    """Age in years when `raw` is a bare age, else None.

    Never guesses. A value that does not match cleanly, or that falls outside
    a plausible human lifespan, returns None and is left verbatim and
    unannotated -- the same rule the last-seen field follows: an ambiguous
    field is never completed by inference.

    Idempotent: a value that already carries a "(N years old)" hint is declined,
    so it can never be given a second one.
    """
    if not raw:
        return None
    raw = raw.strip()
    if _EXISTING_AGE_HINT_RE.search(raw):
        return None
    m = _BARE_AGE_RE.match(raw)
    if not m:
        return None
    age = int(m.group(1))
    return age if 0 <= age <= _MAX_PLAUSIBLE_AGE else None

# Regex for US short date formats the officer might type into the form:
#   "2/20/26", "02/20/2026", "2-20-26", "2/20/2026"
#   Also handles space as the final separator: "01/08 2026" (officer typo)
_US_DATE_RE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})[/\-\s](\d{2}|\d{4})$")

# Regex for raw time values officers type in "HHMM" or "HHMM AM/PM" style.
# Also handles already-formatted "H:MM AM/PM" (no-op path).
_RAW_HHMM_RE  = re.compile(r"^(\d{3,4})\s*(AM|PM)?$", re.IGNORECASE)
_FMT_TIME_RE  = re.compile(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", re.IGNORECASE)

# Regex for a US date fragment anywhere in a string (non-anchored).
# Used by _normalize_datetime() to find and extract date components from combined
# "HHMM HOURS M/D/YY" strings in the last_seen_datetime field.
# Requires "/" or "-" separators (not space) to avoid false-positives on time digits.
_DATE_FRAGMENT_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2}|\d{4})\b")

# Matches a fully-normalised time string as produced by _normalize_time():
#   "21:30", "04:45 AM", "09:03 PM"
# Used by _normalize_datetime() to confirm a pure-time parse succeeded.
_CLEAN_TIME_RE = re.compile(r"^\d{2}:\d{2}(?:\s+(?:AM|PM))?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def _to_iso_date(date_str: str) -> Optional[str]:
    """
    Try to parse a US-format short date string to ISO 8601 (YYYY-MM-DD).

    Accepts M/D/YY, M/D/YYYY, M-D-YY, M-D-YYYY, and "M/D YYYY" (space before year).
    Also normalises common officer typos before matching:
      - "|" used instead of "/" (e.g. "12|11/25" → "12/11/25", form 9 regression).
    Returns None if the string does not match or the date is invalid.

    Used so the PDF Event Log line 1 has an ISO date that the Event Name
    reconstruction regex in main.py can reliably find (instead of requiring
    DOTALL to scan past the form date to the intake_timestamp on line 2).
    """
    if not date_str:
        return None
    # Normalise officer typos before regex matching:
    #   "|" instead of "/" separator — form 9 regression ("12|11/25" → "12/11/25").
    date_str = date_str.strip().replace("|", "/")
    m = _US_DATE_RE.match(date_str)
    if not m:
        return None
    month_s, day_s, year_s = m.groups()
    year = int(year_s) + (2000 if len(year_s) == 2 else 0)
    try:
        return datetime(year, int(month_s), int(day_s)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _normalize_time(raw: str) -> str:
    """
    Normalize raw time strings typed by officers into a consistent display format.

    Officers type time inconsistently into the PDF form — all of the following
    should produce "04:45 AM":
        "0445 AM"   "0445"   "445 AM"   "4:45 AM"   "04:45 AM"

    Military "HRS" / "HOURS" suffix is stripped before parsing:
        "2200 HRS"   →  "22:00"
        "1300 HOURS" →  "13:00"
        "340HRS"     →  "03:40"

    24-hour times without AM/PM are left in 24-hour format with zero-padded hours:
        "1430"  →  "14:30"
        "0930"  →  "09:30"

    Hours are always zero-padded (HH:MM) so event log entries are unambiguous
    about whether the time was night or day (e.g. "09:03" not "9:03").

    If the value cannot be parsed (e.g. it already includes a date component),
    it is returned as-is so the dispatcher can correct it manually.
    """
    if not raw:
        return raw
    raw = raw.strip()

    # Strip military "HRS" / "HOURS" suffix (e.g. "2200 HRS", "1300 HOURS", "340HRS")
    # before parsing.  Officers use both abbreviations interchangeably.
    raw = re.sub(r"\s*(HRS|HOURS)\s*$", "", raw, flags=re.IGNORECASE)

    # Already has a colon — reformat with zero-padded hour.
    m = _FMT_TIME_RE.match(raw)
    if m:
        h, mi, period = int(m.group(1)), m.group(2), (m.group(3) or "").upper()
        return f"{h:02d}:{mi} {period}".strip() if period else f"{h:02d}:{mi}"

    # Raw HHMM with optional AM/PM (e.g. "0445 AM", "1430").
    m = _RAW_HHMM_RE.match(raw)
    if m:
        digits = m.group(1).zfill(4)          # "445" → "0445"
        h, mi  = int(digits[:2]), digits[2:]   # h=4, mi="45"
        period = (m.group(2) or "").upper()
        return f"{h:02d}:{mi} {period}".strip() if period else f"{h:02d}:{mi}"

    return raw  # unparseable — return as-is, dispatcher corrects


def _normalize_datetime(raw: str) -> str:
    """
    Normalize the `last_seen_datetime` AcroForm field, which officers may fill
    with a time only, a date only, or both in various orderings.

    DESIGN DECISION (do not revert without team discussion): This function
    replaces the direct _normalize_time() call on `last_seen_datetime` in
    build_synthetic_summary(). The v2 form has a single combined "Last Seen
    Date & Time" AcroForm field, unlike the v1 form where only time was
    recorded. _normalize_time() alone cannot handle "2130 HOURS 2/20/26"
    because the HOURS suffix is mid-string (not at the end), so the strip
    regex never fires. See Issue #170.

    Algorithm:
    1. Try pure-time normalisation first (_normalize_time). If the result is
       a clean "HH:MM [AM/PM]" string, the field contains only a time — return it.
    2. Otherwise, search for a US date fragment (M/D/YY or M/D/YYYY with / or -).
       If none found, return whatever _normalize_time produced (best effort).
    3. Extract and ISO-format the date, then normalise the remaining time portion
       separately and recombine as "YYYY-MM-DD HH:MM".

    Supported combined patterns:
        "2130 HOURS 2/20/26"   →  "2026-02-20 21:30"
        "2130 2/20/26"         →  "2026-02-20 21:30"
        "2/20/26 2130"         →  "2026-02-20 21:30"
        "2/20/26 2130 HOURS"   →  "2026-02-20 21:30"
        "2/20/26 21:30"        →  "2026-02-20 21:30"

    Pure-time inputs (existing behaviour — unchanged):
        "2130 HOURS"           →  "21:30"
        "0445 AM"              →  "04:45 AM"
        "1430"                 →  "14:30"

    Unparseable strings are returned as-is.
    """
    if not raw:
        return raw

    # Try pure-time first — handles the common case where the officer typed only
    # a time.  If the result is a clean "HH:MM [AM/PM]" string, done.
    normalized = _normalize_time(raw)
    if _CLEAN_TIME_RE.match(normalized):
        return normalized

    # Pure-time parse didn't produce a clean result.
    # Look for a US date fragment — if present, field is a combined datetime.
    s = raw.strip()
    date_m = _DATE_FRAGMENT_RE.search(s)
    if not date_m:
        # No date fragment — return whatever _normalize_time produced (may have
        # stripped a trailing HRS/HOURS suffix; still better than raw).
        return normalized

    # Convert the date fragment to ISO.
    date_iso = _to_iso_date(date_m.group(0))
    if not date_iso:
        return raw  # Fragment found but unparseable — return original verbatim

    # Remove the date fragment to isolate the time portion.
    time_part = (s[: date_m.start()] + s[date_m.end() :]).strip()

    if time_part:
        return f"{date_iso} {_normalize_time(time_part)}"
    return date_iso  # Date only — no time portion found


def is_pdf(raw_bytes: bytes) -> bool:
    """Return True if raw_bytes begins with the PDF magic signature (%PDF)."""
    return raw_bytes[:4] == PDF_MAGIC


def extract_acroform_fields(pdf_bytes: bytes) -> dict[str, str]:
    """
    Open a PDF from bytes and return a dict of all AcroForm widget field values.

    Checkbox values are either _CHECKED ("Yes") or _UNCHECKED ("Off").
    Text values are plain strings (may be empty).

    Raises ValueError if the PDF has no AcroForm fields — caller should
    return a 400 with guidance to re-send as a filled typed PDF or JPEG photo.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    fields: dict[str, str] = {}
    for page in doc:
        for widget in page.widgets():
            if widget.field_name:
                fields[widget.field_name] = widget.field_value or ""
    doc.close()

    if not fields:
        raise ValueError(
            "This PDF has no fillable form fields. "
            "Please submit a filled v2 form as a typed PDF, "
            "or re-send as a JPEG photo of the completed paper form."
        )

    logger.info("PDF AcroForm extracted: %d fields", len(fields))
    return fields


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _q_answer(yes_val: str, no_val: str) -> str:
    """
    Resolve a YES/NO checkbox pair to an answer string.

    Conventions:
      yes="Yes", no="Off"  → "Yes"
      yes="Off", no="Yes"  → "No"
      both "Off" / blank   → NOT_ANSWERED  (question was skipped)
      both "Yes"           → NOT_ANSWERED  (ambiguous — both boxes checked)
    """
    yes_checked = yes_val == _CHECKED
    no_checked  = no_val  == _CHECKED

    if yes_checked and not no_checked:
        return "Yes"
    if no_checked and not yes_checked:
        return "No"
    # Both off OR both checked → ambiguous
    return NOT_ANSWERED


def _format_q_line(q_num: int, answer: str, fields: dict[str, str]) -> str:
    """
    Build one LPB Questionnaire output line.

    Format: "Q# - ANSWER - Question text [— detail_prefix: detail_value]"
    Detail suffix is omitted when the detail field is empty.
    """
    base_label, detail_field, detail_prefix = _Q_DEFS[q_num]
    label = base_label

    if detail_field:
        detail_value = fields.get(detail_field, "").strip()
        if detail_value:
            label = f"{label} — {detail_prefix}: {detail_value}"

    return f"Q{q_num} - {answer} - {label}"


def _build_at_risk_list(q_answers: dict[int, str], fields: dict[str, str]) -> list[str]:
    """
    Derive at-risk factors from Q answers and detail fields.

    Rules (mirrors what Gemini derives in the JPEG path):
      Q6 Yes  → reason from q6_risk_why (or "At-risk" if blank)
      Q9 Yes  → diagnosis from q9_details (or "Mental health component" if blank)
      Q1 No   → "Unfamiliar with area"
      Q5 Yes  → "Alone"
      Q7 No   → "No proper equipment"
      Q2 No   → "No phone"
    """
    risks: list[str] = []

    if q_answers.get(6) == "Yes":
        reason = fields.get("q6_risk_why", "").strip()
        risks.append(reason if reason else "At-risk")

    if q_answers.get(9) == "Yes":
        detail = fields.get("q9_details", "").strip()
        risks.append(detail if detail else "Mental health component")

    if q_answers.get(1) == "No":
        risks.append("Unfamiliar with area")

    if q_answers.get(5) == "Yes":
        risks.append("Alone")

    if q_answers.get(7) == "No":
        risks.append("No proper equipment")

    if q_answers.get(2) == "No":
        risks.append("No phone")

    return risks


# ---------------------------------------------------------------------------
# Canonical age-from-DOB computation (#765)
# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH. main.py imports this as `_compute_age_from_dob` and
# migration_validation/apply_helpers.py imports it too. It lives HERE, in the
# lower layer, because main.py already imports from pdf_extract and the reverse
# would be circular.
#
# It was moved out of main.py by #765. Before that, this module did its own raw
# year arithmetic with neither the century correction nor the negativity check,
# and emitted "(-29 years old)" on a live 2026-08-18 dementia callout: Python's
# %y cutover is 1969-2068, so 2-digit years 27-68 land in the FUTURE and go
# negative. Two implementations, one correct. The duplication WAS the bug, so
# the fix is one implementation, not two correct ones.
_DOB_FORMATS = (
    "%m/%d/%Y",   # 10/20/2005 (the SJSU regression case; also matches "10/20/2005")
    "%m/%d/%y",   # 09/18/05  (2-digit year)
    "%m-%d-%Y",   # 6-26-2010 (hyphen separator — common in handwritten forms)
    "%m-%d-%y",   # 6-26-10   (hyphen + 2-digit year — surfaced via corpus apply_helpers)
    "%Y-%m-%d",   # ISO 2005-10-20
    "%B %d, %Y",  # October 20, 2005
    "%b %d, %Y",  # Oct 20, 2005
)

def compute_age_from_dob(dob_text: Optional[str], today: _date) -> Optional[int]:
    """Parse a DOB date string; return age in completed years relative to `today`.

    Returns None if `dob_text` is unparseable or yields a date that is still
    in the future after the 2-digit-year past-correction. Strips a trailing
    "(...)" hint from `dob_text` first, so callers may pass either
    "10/20/2005" or the full "10/20/2005 (21 years old)" form.

    2-digit-year disambiguation: %y defaults to the 1969-2068 cutover. For
    DOBs we always prefer the past — if the parsed year ends up in the future
    relative to `today`, subtract 100 (so "01/01/30" on a 2026 today becomes
    1930 → age 96, not 2030 → negative age).
    """
    if not dob_text:
        return None
    candidate = dob_text.split("(", 1)[0].strip()
    if not candidate:
        return None
    parsed = None
    used_2digit_year = False
    # Try-cascade: try each format in turn. ValueError per-format is expected
    # — most formats won't match any given input. If none match, parsed stays
    # None and we return None below (explicit handling, not silent swallow).
    for fmt in _DOB_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt).date()
            used_2digit_year = fmt in ("%m/%d/%y", "%m-%d-%y")
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    # Only apply the year-minus-100 past-correction when the parser actually
    # used %y (2-digit year). A 4-digit year that's already in the future
    # ("09/18/2099") is a data entry error, not a 19xx/20xx ambiguity →
    # return None rather than fabricating a sensible-looking 1999.
    if parsed.year > today.year:
        if not used_2digit_year:
            return None
        try:
            parsed = parsed.replace(year=parsed.year - 100)
        except ValueError:
            return None
    if parsed > today:
        return None
    age = today.year - parsed.year
    if (today.month, today.day) < (parsed.month, parsed.day):
        age -= 1
    return age if age >= 0 else None


# ---------------------------------------------------------------------------
# Synthetic summary builder
# ---------------------------------------------------------------------------

def build_synthetic_summary(
    fields: dict[str, str],
    dispatcher_last_name: str,
    intake_timestamp: str,
) -> str:
    """
    Convert AcroForm field dict into the same structured plain-text format
    that Gemini Pass 1 produces for JPEG forms.

    Output has exactly three sections separated by "---":
      Initial Incident Summary
      ---
      Event Log
      ---
      LPB Questionnaire

    This text feeds directly into the existing geocoding + Overpass + Gemini
    staging pipeline, which only reads specific labeled fields via regex.

    Privacy: No PII is logged. The returned string exists only in memory
    for the duration of the request.
    """

    def _f(key: str) -> str:
        """Return field value stripped of whitespace and carriage-returns, or empty string."""
        return (fields.get(key) or "").replace("\r", "").strip()

    # --- Resolve Q answers up front (needed for both LP section and at-risk) ---
    q_answers: dict[int, str] = {}
    for n in range(1, 13):
        q_answers[n] = _q_answer(_f(f"q{n}_yes"), _f(f"q{n}_no"))

    # --- At-risk list ---
    risks = _build_at_risk_list(q_answers, fields)
    at_risk_str = ", ".join(risks) if risks else "None identified"

    # --- Event log entry 1: request-received timestamp ---
    date_req = _f("date_of_request")
    time_req = _f("time_of_request")
    # Convert form date to ISO format so the Event Name reconstruction regex in main.py
    # can find the ISO date on line 1 without needing DOTALL to scan past it.
    # Officers type dates like "2/20/26" — convert to "2026-02-20" for the Event Log.
    # If the date is already ISO or unparseable, fall back to the raw string.
    date_iso = _to_iso_date(date_req)
    date_for_log = date_iso if date_iso else date_req
    time_for_log = _normalize_time(time_req)
    call_time = f"{date_for_log} {time_for_log}".strip() if (date_for_log or time_for_log) else "[time not recorded]"

    agency    = _f("agency")
    contact   = _f("point_of_contact")
    requester = f"{agency}/{contact}".strip("/") if (agency or contact) else "[requestor not recorded]"

    # -----------------------------------------------------------------------
    # Section 1 — Initial Incident Summary
    # -----------------------------------------------------------------------
    mp_name    = _f("mp_name")
    mp_dob_raw = _f("mp_dob")
    # Append calculated age in the canonical "(N years old)" form. This matches
    # the format Gemini emits on the JPEG path AND the format the downstream
    # extractor `_D4H_RE_DOB` + the canonicalizer `_DOB_AGE_HINT_RE` require.
    # Surfaced by a real dispatch: pre-fix this emitted
    # "(N yo)" — neither regex matched, so DOB + age were silently dropped
    # from the D4H involved-person POST, leaving the Involved tab blank.
    # Only appended when DOB parses as a US date; raw string unchanged otherwise.
    _dob_iso = _to_iso_date(mp_dob_raw)
    if _dob_iso and mp_dob_raw:
        # #765: delegate to the canonical helper above. This block used to do
        # its own year arithmetic with neither a century correction nor a
        # negativity check, and shipped "(-29 years old)" on a live dementia
        # callout. It also used a naive datetime.today() -- UTC on Cloud Run --
        # against a recompute that works in Pacific, so a birthday inside that
        # 7-hour window produced an off-by-one age.
        #
        # A None here means the DOB did not parse or is in the future. Emit the
        # raw value with NO hint rather than a wrong one: a missing hint is
        # visibly missing, an impossible one reads as an answer.
        _today = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles")).date()
        _age = compute_age_from_dob(mp_dob_raw, _today)
        if _age is None:
            mp_dob = mp_dob_raw
        else:
            _unit = "year" if _age == 1 else "years"
            mp_dob = f"{mp_dob_raw} ({_age} {_unit} old)"
    else:
        # #774: no parseable date. If the dispatcher typed a bare AGE, append
        # the canonical hint so the four downstream parsers can read it, and
        # APPEND rather than replace -- what the dispatcher typed is what the
        # requesting agency said, and it is the only record of that.
        #
        # This populates D4H's `age`, not its `dateOfBirth`. There is no birth
        # date to recover from an age and inventing one would be fabrication,
        # so `dateOfBirth` stays correctly null while the information the
        # agency actually supplied stops being dropped.
        _age_only = _bare_age(mp_dob_raw)
        if _age_only is not None:
            _unit = "year" if _age_only == 1 else "years"
            mp_dob = f"{mp_dob_raw.strip()} ({_age_only} {_unit} old)"
        else:
            mp_dob = mp_dob_raw
    mp_address = _f("mp_address")
    mp_wearing = _f("mp_wearing") or "Not recorded"
    mp_with    = _f("mp_with")    or "Not recorded"
    lkp        = _f("last_seen_location")
    # Normalize multi-line LKP to a single line — officers sometimes type a location
    # name on line 1 and the street address on line 2
    # (e.g., "GOOD SAM HOSPITAL\n2425 SAMARITAN DR. SAN JOSE, CA 95124").
    # Joining with a space preserves both on one line so main.py's geocoding cascade
    # and event-name reconstruction can parse the full address.
    if lkp:
        _lkp_lines = [line.strip() for line in lkp.splitlines() if line.strip()]
        lkp = " ".join(_lkp_lines)
        # Strip AcroForm bleed-through: the field overlay can capture the printed
        # form label from the row below (e.g., "Point of Contact (Name and Phone
        # Number):") as a spurious trailing line when the officer types a 2-line
        # address (location name + street).
        #
        # Primary: truncate after the first US zip code that is preceded by alphabetic
        # content — the natural end of a US mailing address.
        # "GOOD SAM HOSPITAL 2000 SAMARITAN DR. SAN JOSE, CA 95124 Point of Contact..."
        #  → "GOOD SAM HOSPITAL 2000 SAMARITAN DR. SAN JOSE, CA 95124"
        #
        # DESIGN DECISION (PR #160): use re.finditer() not re.search() — re.search()
        # returns the FIRST 5-digit match, which for "10000 CALVERT DR. CUPERTINO 95014"
        # is the house number 10410, not the zip 95014.  A US zip always follows the
        # city name, so there must be at least one letter before it.  We iterate
        # matches and skip any that have no alphabetic content before them.
        #
        # Fallback: if no valid zip is found and more than 2 content lines exist,
        # keep only the first two — the third (and beyond) is always a printed form
        # label, never officer address content.
        _zip_m = None
        for _m in re.finditer(r"\b\d{5}(?:-\d{4})?\b", lkp):
            if _m.start() > 0 and re.search(r"[A-Za-z]", lkp[: _m.start()]):
                _zip_m = _m
                break
        if _zip_m:
            lkp = lkp[: _zip_m.end()]
        elif len(_lkp_lines) > 2:
            lkp = " ".join(_lkp_lines[:2])
    staging    = _f("staging_area")
    event_num  = _f("event_number")
    # Collapse embedded newlines: the Request box is a multi-line AcroForm text
    # field and officers use its full height, so the raw value arrives wrapped
    # (1 of 14 populated corpus forms, measured 2026-08-01). Interpolated raw it
    # breaks the one-line "Field: value" shape of this section, and any consumer
    # reading `^Request:\s*(.+)$` would silently keep only the first physical
    # line — on the measured form that would have dropped "SPANISH SPEAKING
    # ONLY" from the tail. Collapsing here keeps the summary line-oriented and
    # makes every downstream reader correct by construction.
    request    = " ".join(_f("request").split())

    # Event Name placeholder — server-side reconstruction in main.py will
    # overwrite this from the LKP field, same as the JPEG path.
    event_name_placeholder = f"[event name — reconstructed server-side from LKP]"

    summary_section = f"""\
Initial Incident Summary:
Event Name: {event_name_placeholder}
Event #: {event_num or "[not recorded]"}
Agency: {agency or "[not recorded]"}
Contact: {contact or "[not recorded]"}
Missing Person: {mp_name or "[not recorded]"}; at-risk: {at_risk_str}
DOB: {mp_dob or "[not recorded]"}
Last Seen At: {_normalize_datetime(_f("last_seen_datetime")) or "[not recorded]"}
Last Known Position: {lkp or "[not recorded]"}
Residence Address: {mp_address or "Not recorded"}
Last Seen Wearing: {mp_wearing}
Last Seen With: {mp_with}
Request: {request or "[not recorded]"}
Staging Area for Resources: {staging}
CalTopo Map ID:
Dispatcher: {dispatcher_last_name}"""

    # -----------------------------------------------------------------------
    # Section 2 — Event Log
    # -----------------------------------------------------------------------
    event_log_section = f"""\
Event Log:
{call_time} - Request received from {requester}
{intake_timestamp} - Intake form processed via PDF; Initial Incident Summary created"""

    # -----------------------------------------------------------------------
    # Section 3 — LPB Questionnaire
    # -----------------------------------------------------------------------
    q_lines = [_format_q_line(n, q_answers[n], fields) for n in range(1, 13)]
    lpb_section = "LPB Questionnaire:\n" + "\n".join(q_lines)

    # -----------------------------------------------------------------------
    # Assemble with section separators (two "---" between three sections)
    # -----------------------------------------------------------------------
    return "\n".join([
        summary_section,
        "---",
        event_log_section,
        "---",
        lpb_section,
    ])
