"""
test_pdf_extract.py — Unit tests for pdf_extract.py

Run with:
    pip install pymupdf pytest
    pytest backend/test_pdf_extract.py -v

Coverage:
  - is_pdf() magic byte detection
  - _q_answer() YES/NO/NOT ANSWERED resolution
  - _format_q_line() output format
  - _build_at_risk_list() risk derivation logic
  - build_synthetic_summary() — structure, field mapping, at-risk integration
  - extract_acroform_fields() — integration test with real PDF (skipped if PDF unavailable)
"""

import re
import sys
import os

import pytest

# Allow running from repo root: backend/ is not a package, so add it to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from pdf_extract import (
    is_pdf,
    _q_answer,
    _format_q_line,
    _build_at_risk_list,
    build_synthetic_summary,
    extract_acroform_fields,
    _to_iso_date,
    _bare_age,
    _normalize_time,
    _normalize_datetime,
    NOT_ANSWERED,
    PDF_MAGIC,
    MAX_PDF_BYTES,
)


# ---------------------------------------------------------------------------
# is_pdf()
# ---------------------------------------------------------------------------

class TestIsPdf:
    def test_pdf_magic_bytes(self):
        assert is_pdf(b"%PDF-1.4 rest of file") is True

    def test_empty_bytes(self):
        assert is_pdf(b"") is False

    def test_jpeg_bytes(self):
        assert is_pdf(b"\xff\xd8\xff\xe0") is False

    def test_short_bytes_not_pdf(self):
        assert is_pdf(b"%PD") is False

    def test_pdf_magic_constant(self):
        assert PDF_MAGIC == b"%PDF"

    def test_max_pdf_bytes_constant(self):
        assert MAX_PDF_BYTES == 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# _q_answer()
# ---------------------------------------------------------------------------

class TestQAnswer:
    def test_yes_checked(self):
        assert _q_answer("Yes", "Off") == "Yes"

    def test_no_checked(self):
        assert _q_answer("Off", "Yes") == "No"

    def test_both_off(self):
        assert _q_answer("Off", "Off") == NOT_ANSWERED

    def test_both_yes_ambiguous(self):
        assert _q_answer("Yes", "Yes") == NOT_ANSWERED

    def test_empty_values(self):
        assert _q_answer("", "") == NOT_ANSWERED

    def test_not_answered_constant(self):
        assert NOT_ANSWERED == "NOT ANSWERED (flag for follow-up)"


# ---------------------------------------------------------------------------
# _format_q_line()
# ---------------------------------------------------------------------------

class TestFormatQLine:
    def _fields(self, **kwargs):
        return kwargs

    def test_q1_no_detail(self):
        line = _format_q_line(1, "Yes", {})
        assert line == "Q1 - Yes - Familiar with area"

    def test_q2_with_phone_number(self):
        fields = {"q2_phone": "408-555-1234"}
        line = _format_q_line(2, "Yes", fields)
        assert line == "Q2 - Yes - Has phone — number: 408-555-1234"

    def test_q2_no_phone_detail_empty(self):
        # No phone number recorded — detail suffix omitted
        line = _format_q_line(2, "Yes", {})
        assert line == "Q2 - Yes - Has phone"

    def test_q6_at_risk_with_reason(self):
        fields = {"q6_risk_why": "Dementia — wanders"}
        line = _format_q_line(6, "Yes", fields)
        assert line == "Q6 - Yes - At-risk — reason: Dementia — wanders"

    def test_q8_non_english_with_languages(self):
        fields = {"q8_languages": "Spanish, Vietnamese"}
        line = _format_q_line(8, "No", fields)
        assert line == "Q8 - No - Speaks English — languages: Spanish, Vietnamese"

    def test_not_answered_propagates(self):
        line = _format_q_line(3, NOT_ANSWERED, {})
        assert NOT_ANSWERED in line
        assert "Q3" in line

    def test_format_starts_with_q_number(self):
        for n in range(1, 13):
            line = _format_q_line(n, "Yes", {})
            assert line.startswith(f"Q{n} - ")

    def test_format_answer_before_question(self):
        line = _format_q_line(1, "No", {})
        parts = line.split(" - ")
        assert parts[0] == "Q1"
        assert parts[1] == "No"
        assert "Familiar" in parts[2]


# ---------------------------------------------------------------------------
# _build_at_risk_list()
# ---------------------------------------------------------------------------

class TestBuildAtRiskList:
    def _answers(self, **kwargs):
        return {int(k): v for k, v in kwargs.items()}

    def test_no_risks(self):
        answers = {n: "No" for n in range(1, 13)}
        answers[1] = "Yes"  # Familiar with area (no risk)
        answers[2] = "Yes"  # Has phone (no risk)
        answers[5] = "No"   # Not alone
        answers[6] = "No"   # Not at-risk
        answers[7] = "Yes"  # Has equipment
        answers[9] = "No"   # No mental health
        risks = _build_at_risk_list(answers, {})
        assert risks == []

    def test_q6_yes_with_reason(self):
        answers = {n: "No" for n in range(1, 13)}
        answers[6] = "Yes"
        fields = {"q6_risk_why": "Alzheimer's"}
        risks = _build_at_risk_list(answers, fields)
        assert "Alzheimer's" in risks

    def test_q6_yes_no_reason_fallback(self):
        answers = {n: "No" for n in range(1, 13)}
        answers[6] = "Yes"
        risks = _build_at_risk_list(answers, {})
        assert "At-risk" in risks

    def test_q9_yes_with_diagnosis(self):
        answers = {n: "No" for n in range(1, 13)}
        answers[9] = "Yes"
        fields = {"q9_details": "Bipolar disorder"}
        risks = _build_at_risk_list(answers, fields)
        assert "Bipolar disorder" in risks

    def test_q1_no_unfamiliar(self):
        answers = {n: "Yes" for n in range(1, 13)}
        answers[1] = "No"
        risks = _build_at_risk_list(answers, {})
        assert "Unfamiliar with area" in risks

    def test_q5_yes_alone(self):
        answers = {n: "No" for n in range(1, 13)}
        answers[5] = "Yes"
        risks = _build_at_risk_list(answers, {})
        assert "Alone" in risks

    def test_q7_no_no_equipment(self):
        answers = {n: "Yes" for n in range(1, 13)}
        answers[7] = "No"
        risks = _build_at_risk_list(answers, {})
        assert "No proper equipment" in risks

    def test_q2_no_no_phone(self):
        answers = {n: "Yes" for n in range(1, 13)}
        answers[2] = "No"
        risks = _build_at_risk_list(answers, {})
        assert "No phone" in risks

    def test_multiple_risks_combined(self):
        answers = {n: "No" for n in range(1, 13)}
        answers[1] = "No"   # Unfamiliar
        answers[5] = "Yes"  # Alone
        answers[6] = "Yes"  # At-risk
        answers[9] = "Yes"  # Mental health
        fields = {"q6_risk_why": "Dementia", "q9_details": "Bipolar"}
        risks = _build_at_risk_list(answers, fields)
        assert "Dementia" in risks
        assert "Bipolar" in risks
        assert "Unfamiliar with area" in risks
        assert "Alone" in risks


# ---------------------------------------------------------------------------
# build_synthetic_summary()
# ---------------------------------------------------------------------------

_FULL_FIELDS = {
    "date_of_request": "3/3/26",
    "time_of_request": "14:30",
    "last_seen_datetime": "3/3/26 10:00",
    "last_seen_location": "1200 East Calaveras Blvd, Milpitas, CA 95035",
    "point_of_contact": "Sgt. Smith 408-555-0001",
    "staging_area": "Cardoza Park, Milpitas",
    "agency": "MPD",
    "event_number": "2026-0303-001",
    "request": "Locate missing person",
    "mp_name": "Jane Doe",
    "mp_dob": "1/15/1960",
    "mp_address": "100 Main St, Milpitas, CA 95035",
    "mp_wearing": "Blue jacket, jeans",
    "mp_with": "Alone",
    # Checkboxes: Q1 Yes, Q2 Yes (phone), Q3 No, Q4 Yes, Q5 No, Q6 No,
    #             Q7 Yes, Q8 Yes, Q9 No, Q10 No, Q11 Yes, Q12 No
    "q1_yes": "Yes", "q1_no": "Off",
    "q2_yes": "Yes", "q2_no": "Off", "q2_phone": "415-555-9999",
    "q3_yes": "Off", "q3_no": "Yes",
    "q4_yes": "Yes", "q4_no": "Off", "q4_mups_date": "3/3/26",
    "q5_yes": "Off", "q5_no": "Yes",
    "q6_yes": "Off", "q6_no": "Yes",
    "q7_yes": "Yes", "q7_no": "Off",
    "q8_yes": "Yes", "q8_no": "Off",
    "q9_yes": "Off", "q9_no": "Yes",
    "q10_yes": "Off", "q10_no": "Yes",
    "q11_yes": "Yes", "q11_no": "Off",
    "q12_yes": "Off", "q12_no": "Yes",
}


class TestBareAgeInDobField:
    """Issue #774 — a bare AGE typed into the DOB field.

    Live 2026-08-23 mutual aid: the requesting agency supplied an age, not a
    date of birth, so the AcroForm value was "24 yo". `_to_iso_date()` declined
    it and the raw string passed straight through, which four independent
    downstream parsers could not read: the Slack MP line rendered "?yo
    Unknown", the Everbridge body said "unknown age person" (hand-patched into
    "a missing 24 age person" and shipped to every responder), and the D4H
    Involved tab came out with a blank DOB. Only the free-text D4H Description
    survived, because nothing parsed it.

    The JPEG path does not have this bug: gemini.py instructs the model to emit
    "(N years old)", so Gemini normalizes a bare age. The PDF path had no
    equivalent step.
    """

    def _summary(self, dob_raw):
        fields = dict(_FULL_FIELDS)
        fields["mp_dob"] = dob_raw
        return build_synthetic_summary(
            fields, dispatcher_last_name="Burns", intake_timestamp="2026-03-03 14:30",
        )

    def _dob_line(self, dob_raw):
        return next(
            l for l in self._summary(dob_raw).splitlines() if l.startswith("DOB:")
        )

    # -- the helper --------------------------------------------------------

    @pytest.mark.parametrize("raw,expected", [
        ("24 yo", 24), ("24yo", 24), ("24 y/o", 24), ("24 years old", 24),
        ("24 years", 24), ("24 yrs", 24), ("24", 24), ("age 24", 24),
        ("Age: 24", 24), ("1 yo", 1), ("0", 0), ("120", 120),
        # Straight from the real corpus (test_forms FILLABLE-4). This IS the
        # #774 situation stated twice -- the agency gave an age BECAUSE it had
        # no date of birth -- so declining it for the parenthetical would
        # reject the clearest example of the case.
        ("84 YEARS OLD (DOB UNKNOWN)", 84),
        ("24 (approx)", 24),
    ])
    def test_recognized_shapes(self, raw, expected):
        assert _bare_age(raw) == expected

    @pytest.mark.parametrize("raw", [
        "121",          # outside a plausible human lifespan
        "1995", "2005", # a birth YEAR is not an age -- the digit cap is what stops this
        "12/11/25",     # a real date never reaches this branch, but prove it anyway
        "24 months",    # an infant's age in months; converting it would fabricate years
        "~24", "24 or 25", "mid 20s",   # genuinely ambiguous -- never completed by inference
        "", "   ", "abc", "[not recorded]",
    ])
    def test_rejected_shapes_stay_none(self, raw):
        assert _bare_age(raw) is None

    # -- the summary line --------------------------------------------------

    def test_canonical_hint_is_appended(self):
        assert self._dob_line("24 yo") == "DOB: 24 yo (24 years old)"

    def test_dispatcher_text_is_preserved_not_replaced(self):
        """What the dispatcher typed is what the requesting agency said, and on
        a mutual-aid intake it is the only record of it."""
        assert "24 yo" in self._dob_line("24 yo")

    def test_age_one_is_singular(self):
        """Matches _rewrite_dob_age_hint's grammatical-singular rule in main.py,
        which already pins "(1 year old)" for the Gemini-emitted form."""
        assert self._dob_line("1 yo") == "DOB: 1 yo (1 year old)"

    def test_real_date_behaviour_is_unchanged(self):
        """The existing branch still owns any parseable US date."""
        line = self._dob_line("1/15/1960")
        assert line.startswith("DOB: 1/15/1960 (")
        assert "years old)" in line

    def test_unparseable_and_not_an_age_passes_through_verbatim(self):
        assert self._dob_line("see attached") == "DOB: see attached"

    def test_hint_not_doubled_when_already_present(self):
        """Idempotency, and it is load-bearing rather than incidental: the
        pattern admits a trailing parenthetical so that "84 YEARS OLD (DOB
        UNKNOWN)" is accepted, which means "24 (24 years old)" is structurally
        acceptable too and only the hint check declines it."""
        assert self._dob_line("24 (24 years old)") == "DOB: 24 (24 years old)"
        assert _bare_age("24 (24 years old)") is None

    def test_corpus_shape_gets_the_hint(self):
        line = self._dob_line("84 YEARS OLD (DOB UNKNOWN)")
        assert line == "DOB: 84 YEARS OLD (DOB UNKNOWN) (84 years old)"

    def test_date_with_a_non_canonical_age_is_left_alone(self):
        """test_forms FILLABLE-5 carries "12/03/1969 (57)". The trailing
        parenthetical defeats _to_iso_date(), so it reaches this branch -- and
        it must NOT be read as an age, because 12 is a month. Tracked
        separately: the date parser, not the age helper, is what fails there.
        """
        assert _bare_age("12/03/1969 (57)") is None
        assert self._dob_line("12/03/1969 (57)") == "DOB: 12/03/1969 (57)"

    # -- the surfaces this exists to serve ---------------------------------

    def test_line_matches_the_shape_the_d4h_extractor_requires(self):
        """main.py::_D4H_RE_DOB. Mirrored here rather than imported because
        main.py is not importable under local pytest. The age is what carries
        the information -- dateOfBirth stays correctly empty, because there is
        no birth date to recover from an age and inventing one is fabrication.
        """
        d4h_re = re.compile(r"^DOB:\s*([^(\n]+?)\s*(?:\((\d+)\s*[^)]*\))?\s*$", re.MULTILINE)
        m = d4h_re.search(self._summary("24 yo"))
        assert m is not None
        assert m.group(2) == "24", "D4H would receive no age for a bare-age intake"
        assert m.group(1).strip() == "24 yo"

    def test_line_matches_the_shape_the_frontend_age_parser_requires(self):
        r"""index.html::_ebParseAgeFromDob keys on /\((\d+)\s+years?\s+old\)/i.
        This is the parser that produced "?yo Unknown" on the Slack pin and
        "unknown age person" in the Everbridge body.
        """
        assert re.search(r"\((\d+)\s+years?\s+old\)", self._dob_line("24 yo")).group(1) == "24"


class TestBuildSyntheticSummary:
    def _summary(self, fields=None, last_name="Burns", timestamp="2026-03-03 14:30"):
        return build_synthetic_summary(
            fields or _FULL_FIELDS,
            dispatcher_last_name=last_name,
            intake_timestamp=timestamp,
        )

    def test_has_three_sections_separated_by_dashes(self):
        s = self._summary()
        parts = s.split("\n---\n")
        assert len(parts) == 3, f"Expected 3 sections, got {len(parts)}"

    def test_section_order(self):
        s = self._summary()
        parts = s.split("\n---\n")
        assert parts[0].startswith("Initial Incident Summary:")
        assert parts[1].startswith("Event Log:")
        assert parts[2].startswith("LPB Questionnaire:")

    def test_mp_name_in_summary(self):
        s = self._summary()
        assert "Jane Doe" in s

    def test_dob_in_summary(self):
        s = self._summary()
        assert "1/15/1960" in s

    def test_lkp_in_summary(self):
        s = self._summary()
        assert "1200 East Calaveras Blvd" in s

    def test_agency_in_summary(self):
        s = self._summary()
        assert "MPD" in s

    def test_event_number_in_summary(self):
        s = self._summary()
        assert "2026-0303-001" in s

    def test_staging_area_in_summary(self):
        s = self._summary()
        assert "Cardoza Park, Milpitas" in s

    def test_dispatcher_last_name_in_summary(self):
        s = self._summary(last_name="Burns")
        assert "Burns" in s

    def test_request_field_in_summary(self):
        s = self._summary()
        assert "Locate missing person" in s

    def test_mp_wearing_in_summary(self):
        s = self._summary()
        assert "Blue jacket, jeans" in s

    def test_mp_with_in_summary(self):
        s = self._summary()
        assert "Alone" in s

    def test_at_risk_none_identified_when_no_risks(self):
        s = self._summary()
        assert "None identified" in s

    def test_at_risk_populated_when_q6_yes(self):
        fields = dict(_FULL_FIELDS)
        fields["q6_yes"] = "Yes"
        fields["q6_no"] = "Off"
        fields["q6_risk_why"] = "Dementia"
        s = self._summary(fields=fields)
        assert "Dementia" in s
        assert "None identified" not in s

    def test_q_format_correct(self):
        s = self._summary()
        q_section = s.split("\n---\n")[2]
        # Check Q1 Yes
        assert "Q1 - Yes - Familiar with area" in q_section

    def test_q2_detail_included(self):
        s = self._summary()
        q_section = s.split("\n---\n")[2]
        assert "Q2 - Yes - Has phone — number: 415-555-9999" in q_section

    def test_q4_date_included(self):
        s = self._summary()
        q_section = s.split("\n---\n")[2]
        assert "Q4 - Yes - Entered into MUPS — date: 3/3/26" in q_section

    def test_all_12_q_lines_present(self):
        s = self._summary()
        q_section = s.split("\n---\n")[2]
        for n in range(1, 13):
            assert f"Q{n} -" in q_section, f"Q{n} not found in LPB section"

    def test_event_log_contains_request_received(self):
        s = self._summary()
        event_log = s.split("\n---\n")[1]
        assert "Request received" in event_log

    def test_event_log_contains_intake_timestamp(self):
        s = self._summary()
        event_log = s.split("\n---\n")[1]
        assert "2026-03-03 14:30" in event_log

    def test_empty_fields_use_placeholders(self):
        fields = {k: "" for k in _FULL_FIELDS}
        s = self._summary(fields=fields)
        assert "[not recorded]" in s

    def test_mp_wearing_defaults_not_recorded(self):
        fields = dict(_FULL_FIELDS)
        fields["mp_wearing"] = ""
        s = self._summary(fields=fields)
        assert "Not recorded" in s

    def test_lkp_5digit_house_number_not_truncated(self):
        """Form 11 regression: '10000 CALVERT DR. CUPERTINO 95014' — house number 10410
        is 5 digits and was incorrectly matched as the zip code, truncating the address
        to just '10410'. The zip regex must skip matches with no alphabetic content before
        them (city always precedes zip; house number is before any letters)."""
        fields = dict(_FULL_FIELDS)
        fields["last_seen_location"] = "10000 CALVERT DR. CUPERTINO 95014"
        s = self._summary(fields=fields)
        assert "Last Known Position: 10000 CALVERT DR. CUPERTINO 95014" in s

    def test_lkp_zip_truncation_removes_bleed_through(self):
        """AcroForm bleed-through: zip truncation should strip the printed form label
        that follows a 2-line LKP field (regression for PR #153 / GOOD SAM case)."""
        fields = dict(_FULL_FIELDS)
        # Simulate bleed-through: real address + printed label below field
        fields["last_seen_location"] = (
            "GOOD SAM HOSPITAL\n2425 SAMARITAN DR. SAN JOSE, CA 95124\n"
            "Point of Contact (Name and Phone Number):\n"
        )
        s = self._summary(fields=fields)
        lkp_line = next(l for l in s.splitlines() if l.startswith("Last Known Position:"))
        assert "95124" in lkp_line
        assert "Point of Contact" not in lkp_line


# ---------------------------------------------------------------------------
# extract_acroform_fields() — integration test with real PDF
# ---------------------------------------------------------------------------

_FILLABLE_PDF_PATHS = [
    os.path.join(os.path.dirname(__file__), "..", "forms", "SAR Callout Form v2 (generic, fillable).pdf"),
    os.path.join(os.path.dirname(__file__), "..", "forms", "SAR Dispatch Form v2 (fillable).pdf"),
    os.path.join(os.path.dirname(__file__), "..", "research", "SAR Dispatch Form v2 FILLABLE.pdf"),
]


def _find_fillable_pdf():
    for p in _FILLABLE_PDF_PATHS:
        if os.path.exists(p):
            return p
    return None


@pytest.mark.skipif(_find_fillable_pdf() is None, reason="Fillable PDF not present in repo")
class TestExtractAcroformFields:
    def test_fields_returned_dict(self):
        pdf_path = _find_fillable_pdf()
        with open(pdf_path, "rb") as f:
            raw = f.read()
        fields = extract_acroform_fields(raw)
        assert isinstance(fields, dict)

    def test_expected_checkbox_fields_present(self):
        pdf_path = _find_fillable_pdf()
        with open(pdf_path, "rb") as f:
            raw = f.read()
        fields = extract_acroform_fields(raw)
        for n in range(1, 13):
            assert f"q{n}_yes" in fields, f"Missing q{n}_yes"
            assert f"q{n}_no" in fields, f"Missing q{n}_no"

    def test_expected_text_fields_present(self):
        pdf_path = _find_fillable_pdf()
        with open(pdf_path, "rb") as f:
            raw = f.read()
        fields = extract_acroform_fields(raw)
        expected = [
            "mp_name", "mp_dob", "mp_address", "mp_wearing", "mp_with",
            "last_seen_location", "last_seen_datetime",
            "agency", "event_number",
            "date_of_request", "time_of_request",
            "point_of_contact", "staging_area",
        ]
        for field in expected:
            assert field in fields, f"Missing field: {field}"

    def test_total_field_count(self):
        pdf_path = _find_fillable_pdf()
        with open(pdf_path, "rb") as f:
            raw = f.read()
        fields = extract_acroform_fields(raw)
        # 47 fields confirmed in pdf_extract.py docstring
        assert len(fields) >= 40, f"Expected ≥40 fields, got {len(fields)}"

    def test_empty_pdf_raises_value_error(self):
        """A valid PDF with no AcroForm fields should raise ValueError."""
        # Use a minimal valid PDF with no widgets
        minimal_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n"
            b"0000000058 00000 n\n0000000115 00000 n\n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
        )
        with pytest.raises(ValueError, match="no fillable form fields"):
            extract_acroform_fields(minimal_pdf)


# ---------------------------------------------------------------------------
# _to_iso_date()
# ---------------------------------------------------------------------------


class TestToIsoDate:
    def test_slash_2digit_year(self):
        assert _to_iso_date("1/8/26") == "2026-01-08"

    def test_slash_4digit_year(self):
        assert _to_iso_date("01/08/2026") == "2026-01-08"

    def test_dash_separator(self):
        assert _to_iso_date("2-20-2026") == "2026-02-20"

    def test_space_before_year(self):
        """Form 7 regression: officer typed '01/08 2026' with space before year."""
        assert _to_iso_date("01/08 2026") == "2026-01-08"

    def test_space_before_2digit_year(self):
        assert _to_iso_date("3/15 26") == "2026-03-15"

    def test_invalid_returns_none(self):
        assert _to_iso_date("not-a-date") is None

    def test_empty_returns_none(self):
        assert _to_iso_date("") is None

    def test_invalid_date_returns_none(self):
        assert _to_iso_date("13/32/2026") is None  # month=13 is invalid

    def test_pipe_separator_2digit_year(self):
        """Form 9 regression: officer typed '12|11/25' with pipe instead of slash."""
        assert _to_iso_date("12|11/25") == "2025-12-11"

    def test_pipe_separator_4digit_year(self):
        """Pipe normalisation works for 4-digit years too."""
        assert _to_iso_date("12|11|2025") == "2025-12-11"


# ---------------------------------------------------------------------------
# _normalize_time()
# ---------------------------------------------------------------------------


class TestNormalizeTime:
    def test_hhmm_24h(self):
        assert _normalize_time("1430") == "14:30"

    def test_hhmm_midnight(self):
        assert _normalize_time("0000") == "00:00"

    def test_hhmm_am(self):
        assert _normalize_time("0445 AM") == "04:45 AM"

    def test_hhmm_pm(self):
        assert _normalize_time("1200 PM") == "12:00 PM"

    def test_already_formatted(self):
        # Already-formatted times go through the FMT_TIME_RE branch, which also zero-pads.
        assert _normalize_time("9:03") == "09:03"

    def test_already_formatted_ampm(self):
        assert _normalize_time("4:45 AM") == "04:45 AM"

    def test_hrs_suffix_stripped(self):
        """Military 'HRS' suffix (e.g. '2200 HRS') should be normalized."""
        assert _normalize_time("2200 HRS") == "22:00"

    def test_hrs_no_space(self):
        """'340HRS' — 3-digit time with no space before HRS; strips suffix, parses to 03:40."""
        assert _normalize_time("340HRS") == "03:40"

    def test_hours_suffix_stripped(self):
        """Form 9/12 regression: '1300 HOURS' — officers write full word instead of HRS."""
        assert _normalize_time("1300 HOURS") == "13:00"

    def test_hours_suffix_stripped_lower(self):
        """'2130 hours' — lowercase HOURS suffix should also be stripped."""
        assert _normalize_time("2130 hours") == "21:30"

    def test_hours_suffix_4digit_ampm(self):
        """'0445 HOURS' — AM/PM absent so stays in 24h display."""
        assert _normalize_time("0445 HOURS") == "04:45"

    def test_zero_padded_hour(self):
        """Hours must be zero-padded: '0800' → '08:00', not '8:00'."""
        assert _normalize_time("0800") == "08:00"

    def test_unparseable_passthrough(self):
        """Unparseable strings should be returned as-is rather than silently blanked."""
        assert _normalize_time("unknown") == "unknown"


# ---------------------------------------------------------------------------
# _normalize_datetime()
# ---------------------------------------------------------------------------

class TestNormalizeDatetime:
    # ---- Pure-time inputs — should delegate to _normalize_time() unchanged ----

    def test_pure_time_hhmm(self):
        """Pure HHMM — delegates to _normalize_time(), returns HH:MM."""
        assert _normalize_datetime("2130") == "21:30"

    def test_pure_time_hhmm_hours_suffix(self):
        """Pure time with HOURS suffix."""
        assert _normalize_datetime("2130 HOURS") == "21:30"

    def test_pure_time_hhmm_hrs_suffix(self):
        """Pure time with HRS suffix."""
        assert _normalize_datetime("2200 HRS") == "22:00"

    def test_pure_time_ampm(self):
        """Pure time with AM/PM."""
        assert _normalize_datetime("0445 AM") == "04:45 AM"

    def test_pure_time_already_formatted(self):
        """Already-formatted HH:MM is returned with zero-padded hour."""
        assert _normalize_datetime("9:30") == "09:30"

    def test_pure_time_already_formatted_ampm(self):
        """Already-formatted HH:MM AM/PM returned unchanged (zero-padded)."""
        assert _normalize_datetime("4:45 AM") == "04:45 AM"

    # ---- Combined date+time inputs (Issue #170) ----

    def test_time_hours_then_date(self):
        """'2130 HOURS 2/20/26' — the exact bug pattern from form 13 corrected."""
        assert _normalize_datetime("2130 HOURS 2/20/26") == "2026-02-20 21:30"

    def test_time_then_date_no_suffix(self):
        """'2130 2/20/26' — time before date, no HOURS suffix."""
        assert _normalize_datetime("2130 2/20/26") == "2026-02-20 21:30"

    def test_date_then_time(self):
        """'2/20/26 2130' — date before time (officer enters date first)."""
        assert _normalize_datetime("2/20/26 2130") == "2026-02-20 21:30"

    def test_date_then_time_with_hours(self):
        """'2/20/26 2130 HOURS' — date first, HOURS suffix at end."""
        assert _normalize_datetime("2/20/26 2130 HOURS") == "2026-02-20 21:30"

    def test_date_then_formatted_time(self):
        """'2/20/26 21:30' — date first, time already formatted."""
        assert _normalize_datetime("2/20/26 21:30") == "2026-02-20 21:30"

    def test_date_4digit_year(self):
        """4-digit year in date component."""
        assert _normalize_datetime("2130 HOURS 2/20/2026") == "2026-02-20 21:30"

    def test_date_with_dashes(self):
        """Date uses dashes as separator."""
        assert _normalize_datetime("2130 2-20-26") == "2026-02-20 21:30"

    def test_early_morning_time(self):
        """Edge case: 0326 (time of this incident's request)."""
        assert _normalize_datetime("0326 2/20/26") == "2026-02-20 03:26"

    def test_midnight(self):
        """Midnight edge case."""
        assert _normalize_datetime("0000 1/1/26") == "2026-01-01 00:00"

    # ---- Unparseable — return as-is ----

    def test_unparseable_passthrough(self):
        """Strings that can't be parsed at all are returned verbatim."""
        assert _normalize_datetime("unknown") == "unknown"

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert _normalize_datetime("") == ""

    def test_date_only(self):
        """Date with no time component returns ISO date."""
        assert _normalize_datetime("2/20/26") == "2026-02-20"
